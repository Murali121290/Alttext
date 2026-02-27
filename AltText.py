import os
import fitz  # PyMuPDF
import io
import sqlite3
import psycopg2
import psycopg2.extras
import threading
import datetime
import time
from concurrent.futures import ThreadPoolExecutor
from PIL import Image
from flask import Flask, request, send_file, jsonify, render_template, g, redirect as flask_redirect, flash, url_for
from openpyxl import Workbook
import google.genai as genai
from utils.prompt_assets import SYSTEM_PROMPT
from utils.qc_prompt import QC_VALIDATION_PROMPT
from werkzeug.security import generate_password_hash, check_password_hash
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from functools import wraps
import logging

# ---------------- LOGGING CONFIG ----------------
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("alttext_processing.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ---------------- CONFIG ----------------
from dotenv import load_dotenv
import json
import re

load_dotenv()
# genai.configure is no longer needed in new SDK, using Client instead.
# Using a valid model from the available list or user preference
MODEL_NAME = "gemini-3-flash-preview" 

UPLOAD_FOLDER = "uploads"
OUTPUT_FOLDER = "outputs"
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/alttext")

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(OUTPUT_FOLDER, exist_ok=True)

# Remove local SQLite dir check
# if os.path.dirname(DB_PATH):
#    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

# ---------------- DATABASE ABSTRACTION ----------------
# Support both PostgreSQL (Production) and SQLite (Local Dev fallback)

DB_TYPE = "postgres"  # Default to try
IS_SQLITE = False

def get_db_connection():
    global DB_TYPE, IS_SQLITE
    
    # Try PostgreSQL first if configured
    if DB_TYPE == "postgres":
        for attempt in range(5):
            try:
                conn = psycopg2.connect(DATABASE_URL)
                return conn
            except psycopg2.OperationalError:
                if attempt < 4:
                    time.sleep(2)
                    continue
                print("\n[WARNING] PostgreSQL connection failed after retries (Docker not running?).")
                print("[INFO] Falling back to local SQLite database (alttext.db).\n")
                DB_TYPE = "sqlite"
                IS_SQLITE = True
            
    # Fallback to SQLite
    conn = sqlite3.connect("alttext.db")
    conn.row_factory = sqlite3.Row
    return conn

def get_db():
    db = getattr(g, '_database', None)
    if db is None:
        db = g._database = get_db_connection()
    return db

def query_db(query, args=(), one=False, commit=False, return_id=False, conn=None):
    """
    Universal query executor for likely differences between PG and SQLite.
    Handles placeholders (%s vs ?) and ID retrieval.
    """
    if conn is None:
        conn = get_db()
    
    # 1. Handle Placeholders
    # Postgres uses %s, SQLite uses ?
    # We write queries with %s in code, convert to ? if SQLite
    if IS_SQLITE:
        query = query.replace('%s', '?')
        # Handle PostgreSQL specific syntax removal for SQLite if needed
        query = query.replace('RETURNING id', '') 
        query = query.replace('CURRENT_DATE', "date('now')")
    
    # 2. Get Cursor
    if IS_SQLITE:
        cur = conn.cursor()
    else:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        
    # 3. Execute
    try:
        cur.execute(query, args)
        
        # 4. Handle Return ID
        if return_id:
            if IS_SQLITE:
                res = cur.lastrowid
            else:
                # For Postgres, we expect RETURNING id was in the query
                res = cur.fetchone()['id']
        else:
            if one:
                res = cur.fetchone()
            else:
                # For INSERT/UPDATE without return, fetchall might be empty
                if query.strip().upper().startswith(('SELECT', 'RETURNING')):
                    res = cur.fetchall()
                else:
                    res = None

        if commit:
            conn.commit()
            
        cur.close()
        return res
        
    except Exception as e:
        print(f"Query Failed: {query} | Args: {args} | Error: {e}")
        raise e

def init_db():
    """Initializes the database functionality."""
    # Force connection check to set DB_TYPE
    try:
        conn = get_db_connection()
        conn.close()
    except:
        pass
        
    # Define Schema (Differs slightly)
    if IS_SQLITE:
        id_type = "INTEGER PRIMARY KEY AUTOINCREMENT"
        ts_default = "DEFAULT CURRENT_TIMESTAMP"
    else:
        id_type = "SERIAL PRIMARY KEY"
        ts_default = "DEFAULT CURRENT_TIMESTAMP"

    tables = [
        f'''CREATE TABLE IF NOT EXISTS batches (
            id {id_type},
            name TEXT,
            created_at TIMESTAMP {ts_default},
            status TEXT DEFAULT 'pending'
        )''',
        f'''CREATE TABLE IF NOT EXISTS jobs (
            id {id_type},
            batch_id INTEGER,
            filename TEXT,
            status TEXT DEFAULT 'pending',
            output_file TEXT,
            input_tokens INTEGER DEFAULT 0,
            output_tokens INTEGER DEFAULT 0,
            cost REAL DEFAULT 0.0,
            error_msg TEXT,
            created_at TIMESTAMP {ts_default},
            FOREIGN KEY(batch_id) REFERENCES batches(id)
        )''',
        f'''CREATE TABLE IF NOT EXISTS users (
            id {id_type},
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            role TEXT DEFAULT 'user',
            created_at TIMESTAMP {ts_default}
        )'''
    ]

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        for ddl in tables:
            cur.execute(ddl)
        conn.commit()
        
        # Create Admin
        if IS_SQLITE:
             placeholder = "?"
             cur.execute(f"SELECT * FROM users WHERE username = {placeholder}", ('admin',))
        else:
             placeholder = "%s"
             cur.execute(f"SELECT * FROM users WHERE username = {placeholder}", ('admin',))
             
        if not cur.fetchone():
            try:
                enc_pw = generate_password_hash("admin123")
                cur.execute(f"INSERT INTO users (username, password, role) VALUES ({placeholder}, {placeholder}, {placeholder})", 
                              ('admin', enc_pw, 'admin'))
                conn.commit()
            except (sqlite3.IntegrityError, psycopg2.errors.UniqueViolation, psycopg2.IntegrityError):
                # Another worker likely created the user already
                conn.rollback()
                pass
        print(f"Database initialized using {'SQLite' if IS_SQLITE else 'PostgreSQL'}")
    except Exception as e:
        print(f"DB Init Error: {e}")
    finally:
        conn.close()

# Initialize on start
init_db()

# ---------------- APP ----------------
app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "dev_secret_key_change_in_prod") # vital for session

# ---------------- AUTH CONFIG ----------------
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login_page'

class User(UserMixin):
    def __init__(self, id, username, role):
        self.id = id
        self.username = username
        self.role = role

@login_manager.user_loader
def load_user(user_id):
    u = query_db("SELECT * FROM users WHERE id = %s", (user_id,), one=True)
    if u:
        return User(id=u['id'], username=u['username'], role=u['role'])
    return None

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated or current_user.role != 'admin':
            return render_template('error.html', message="Unauthorized. Admin access required."), 403
        return f(*args, **kwargs)
    return decorated_function

@app.teardown_appcontext
def close_connection(exception):
    db = getattr(g, '_database', None)
    if db is not None:
        db.close()


# ---------------- AUTH ROUTES ----------------

@app.route("/login", methods=["GET", "POST"])
def login_page():
    if current_user.is_authenticated:
        return flask_redirect("/")
        
    if request.method == "POST":
        username = request.form.get("username")
        password = request.form.get("password")
        
        user = query_db("SELECT * FROM users WHERE username = %s", (username,), one=True)
        
        # Check password
        if user and check_password_hash(user['password'], password):
            user_obj = User(id=user['id'], username=user['username'], role=user['role'])
            login_user(user_obj)
            return flask_redirect(request.args.get("next") or "/")
        else:
            return render_template("login.html", error="Invalid credentials")
            
    return render_template("login.html")

@app.route("/logout")
@login_required
def logout():
    logout_user()
    return flask_redirect("/login")

@app.route("/register", methods=["GET", "POST"])
def register_page():
    # Public registration
    if current_user.is_authenticated:
        return flask_redirect("/")
        
    if request.method == "POST":
        username = request.form.get("username")
        password = request.form.get("password")
        confirm_password = request.form.get("confirm_password")
        
        if password != confirm_password:
            return render_template("register.html", error="Passwords do not match")
            
        try:
            # Check if user exists
            if query_db("SELECT id FROM users WHERE username = %s", (username,), one=True):
                 return render_template("register.html", error="Username already exists")
                 
            hashed = generate_password_hash(password)
            query_db("INSERT INTO users (username, password, role) VALUES (%s, %s, %s)", 
                    (username, hashed, 'user'), commit=True)
            
            # Login immediately
            user = query_db("SELECT * FROM users WHERE username = %s", (username,), one=True)
            user_obj = User(id=user['id'], username=user['username'], role=user['role'])
            login_user(user_obj)
            
            return flask_redirect("/")
        except Exception as e:
            return render_template("register.html", error=f"Registration failed: {e}")
            
    return render_template("register.html")

# ---------------- ADMIN ROUTES ----------------

@app.route("/admin")
@login_required
@admin_required
def admin_dashboard():
    user_count = query_db("SELECT COUNT(*) as count FROM users", one=True)
    user_count = user_count['count'] if user_count else 0
    
    batch_count = query_db("SELECT COUNT(*) as count FROM batches", one=True)
    batch_count = batch_count['count'] if batch_count else 0

    job_count = query_db("SELECT COUNT(*) as count FROM jobs", one=True)
    job_count = job_count['count'] if job_count else 0

    total_cost_res = query_db("SELECT SUM(cost) as sum FROM jobs", one=True)
    total_cost = total_cost_res['sum'] if total_cost_res and total_cost_res['sum'] else 0.0
    
    # Role stats for the chart
    roles_res = query_db("SELECT role, COUNT(*) as count FROM users GROUP BY role")
    role_stats = {r['role']: r['count'] for r in roles_res} if roles_res else {}

    stats = {
        "user_count": user_count,
        "batch_count": batch_count,
        "job_count": job_count,
        "total_cost": total_cost,
        "role_stats": role_stats
    }
    return render_template("admin_dashboard.html", active_page='admin', stats=stats)

@app.route("/admin/users", methods=["GET"])
@login_required
@admin_required
def admin_users():
    users = query_db("SELECT id, username, role, created_at FROM users ORDER BY created_at DESC")
    return render_template("admin_users.html", active_page='admin_users', users=users)

@app.route("/admin/users/create", methods=["GET", "POST"])
@login_required
@admin_required
def admin_create_user():
    if request.method == "GET":
        return render_template("admin_create_user.html", active_page='admin_users')

    username = request.form.get("username")
    password = request.form.get("password")
    role = request.form.get("role", "user")
    
    try:
        if query_db("SELECT id FROM users WHERE username = %s", (username,), one=True):
             flash(f"Username '{username}' already exists.", "error")
             return flask_redirect("/admin/users/create")

        hashed = generate_password_hash(password)
        query_db("INSERT INTO users (username, password, role) VALUES (%s, %s, %s)", 
                (username, hashed, role), commit=True)
        flash(f"User '{username}' created successfully.", "success")
        return flask_redirect("/admin/users")
    except Exception as e:
        flash(f"Error creating user: {e}", "error")
        return flask_redirect("/admin/users/create")

@app.route("/admin/users/password", methods=["POST"])
@login_required
@admin_required
def admin_change_password():
    user_id = request.form.get("user_id")
    new_password = request.form.get("new_password")
    
    try:
        hashed = generate_password_hash(new_password)
        query_db("UPDATE users SET password = %s WHERE id = %s", (hashed, user_id), commit=True)
        flash("Password updated successfully.", "success")
    except Exception as e:
        flash(f"Error updating password: {e}", "error")
        
    return flask_redirect("/admin/users")

@app.route("/admin/users/role", methods=["POST"])
@login_required
@admin_required
def admin_change_role():
    user_id = request.form.get("user_id")
    role = request.form.get("role")
    
    try:
        query_db("UPDATE users SET role = %s WHERE id = %s", (role, user_id), commit=True)
        flash(f"Role updated to '{role}'.", "success")
    except Exception as e:
        flash(f"Error updating role: {e}", "error")
        
    return flask_redirect("/admin/users")

@app.route("/admin/users/delete/<int:user_id>", methods=["POST"])
@login_required
@admin_required
def admin_delete_user(user_id):
    if user_id == current_user.id:
        flash("Cannot delete yourself.", "error")
        return flask_redirect("/admin/users")
        
    try:
        query_db("DELETE FROM users WHERE id = %s", (user_id,), commit=True)
        flash("User deleted successfully.", "success")
    except Exception as e:
        flash(f"Error deleting user: {e}", "error")
        
    return flask_redirect("/admin/users")

@app.route("/admin/files", methods=["GET"])
@login_required
@admin_required
def admin_files():
    # Listing all jobs as 'files'
    page = request.args.get('page', 1, type=int)
    per_page = 20
    offset = (page - 1) * per_page
    
    jobs = query_db("""
        SELECT j.*, b.name as batch_name 
        FROM jobs j 
        LEFT JOIN batches b ON j.batch_id = b.id 
        ORDER BY j.created_at DESC 
        LIMIT %s OFFSET %s
    """, (per_page, offset))
    
    total_jobs = query_db("SELECT COUNT(*) as count FROM jobs", one=True)['count']
    total_pages = (total_jobs + per_page - 1) // per_page
    
    return render_template("admin_files.html", active_page='admin_files', files=jobs, page=page, total_pages=total_pages)

@app.route("/admin/stats", methods=["GET"])
@login_required
@admin_required
def admin_stats():
    # Detailed stats
    stats = {}
    stats['total_users'] = query_db("SELECT COUNT(*) as count FROM users", one=True)['count']
    stats['total_batches'] = query_db("SELECT COUNT(*) as count FROM batches", one=True)['count']
    stats['total_jobs'] = query_db("SELECT COUNT(*) as count FROM jobs", one=True)['count']
    stats['total_cost'] = query_db("SELECT SUM(cost) as sum FROM jobs", one=True)['sum'] or 0.0
    
    # Recent activity (last 5 batches)
    recent_batches = query_db("SELECT * FROM batches ORDER BY created_at DESC LIMIT 5")
    
    return render_template("admin_stats.html", active_page='admin_stats', stats=stats, recent_batches=recent_batches)

# ---------------- CORE LOGIC ----------------

def clean_alt_text(text):
    """
    Cleans alt text by removing indefinite articles and redundant phrases 
    from the beginning of the text.
    """
    if not text:
        return ""
    
    text = text.strip()
    
    # Remove common prefixes like "a ", "an ", "the "
    lower_text = text.lower()
    if lower_text.startswith("a "):
        text = text[2:].strip()
    elif lower_text.startswith("an "):
        text = text[3:].strip()
    elif lower_text.startswith("the "):
        text = text[4:].strip()
        
    # Remove "Figure X: " prefix variants
    import re
    text = re.sub(r'^(Figure|Fig\.?)\s*\d+[:.]\s*', '', text, flags=re.IGNORECASE)
    
    return text

def process_single_image(img_data, absolute_page_num, run_qc=False):
    """
    Process a single rendered PDF page image.
    Returns: (list of dicts, input_tokens, output_tokens)
    """
    logger.info(f"  Processing Page {absolute_page_num}... (QC: {run_qc})")
    
    items = []
    total_in = 0
    total_out = 0
    
    # --- GEMINI CALL ---
    MAX_RETRIES = 5
    for attempt in range(MAX_RETRIES):
        try:
            image = Image.open(io.BytesIO(img_data))
            
            # Convert to RGB if needed
            if image.mode != "RGB":
                image = image.convert("RGB")
                
            # Resize if absolutely massive (rare for pages but good safety)
            if image.width > 3072 or image.height > 3072:
                image.thumbnail((3072, 3072))
            
            # Use the Centralized System Prompt
            prompt = SYSTEM_PROMPT
            
            if not GEMINI_API_KEY:
                raise ValueError("GEMINI_API_KEY not set")

            client = genai.Client(api_key=GEMINI_API_KEY)
            
            # Add explicit instruction for current page number context
            context_prompt = f"This is Page {absolute_page_num} of the document.\n\n" + prompt
            
            response = client.models.generate_content(
                model=MODEL_NAME,
                contents=[context_prompt, image]
            )
            
            # Parse usage
            if response.usage_metadata:
                total_in += (response.usage_metadata.prompt_token_count or 0)
                total_out += (response.usage_metadata.candidates_token_count or 0)
            
            # Parse JSON
            text_resp = response.text.strip()

            # Robust JSON extraction to handle conversational text
            import re
            json_str = text_resp
            
            # 1. Try to find markdown code block
            code_block = re.search(r'```(?:json)?\s*(.*?)```', text_resp, re.DOTALL)
            if code_block:
                json_str = code_block.group(1).strip()
            else:
                # 2. Heuristic: find first '[' or '{' and last ']' or '}'
                # This handles text like "Here is the JSON: [...]"
                start_idx_list = text_resp.find('[')
                start_idx_obj = text_resp.find('{')
                
                start_idx = -1
                end_chars = ''
                
                if start_idx_list != -1 and (start_idx_obj == -1 or start_idx_list < start_idx_obj):
                     start_idx = start_idx_list
                     end_chars = ']'
                elif start_idx_obj != -1:
                     start_idx = start_idx_obj
                     end_chars = '}'
                     
                if start_idx != -1:
                    last_idx = text_resp.rfind(end_chars)
                    if last_idx != -1 and last_idx > start_idx:
                        json_str = text_resp[start_idx:last_idx+1]

            # Handle potential empty response or non-list
            try:
                data = json.loads(json_str)
            except json.JSONDecodeError:
                logger.error(f"JSON Decode Error on page {absolute_page_num}. Extracted: {json_str[:100]}... Raw: {text_resp[:100]}...")
                # Attempt to fix common issues or just return empty
                data = []

            if isinstance(data, dict):
                data = [data] # Handle single object return edge case
            
            for item in data:
                # Ensure fields exist
                items.append({
                    "page": absolute_page_num, # Force correct page number
                    "figure_number": item.get("figure_number", "Unknown"),
                    "short_alt": item.get("short_alt", ""),
                    "long_alt": item.get("long_alt", ""),
                    "context_type": item.get("context_type", ""),
                    "domain": item.get("domain", "")
                })

            if run_qc:
                logger.info(f"    Running QC for {len(items)} items on page {absolute_page_num}")
                validated_items = []
                for item in items:
                    qc_item = dict(item)
                    if not item.get("long_alt"):
                        validated_items.append(qc_item)
                        continue
                        
                    try:
                        qc_prompt_text = QC_VALIDATION_PROMPT.replace("{alt_text}", item.get("long_alt"))
                        # Re-using the same image and client
                        qc_response = client.models.generate_content(
                            model=MODEL_NAME,
                            contents=[qc_prompt_text, image]
                        )
                        
                        if qc_response.usage_metadata:
                            total_in += (qc_response.usage_metadata.prompt_token_count or 0)
                            total_out += (qc_response.usage_metadata.candidates_token_count or 0)
                            
                        qc_text = qc_response.text.strip()
                        
                        # Extract JSON
                        code_block = re.search(r'```(?:json)?\s*(.*?)```', qc_text, re.DOTALL)
                        qc_json_str = code_block.group(1).strip() if code_block else qc_text
                        
                        start_idx = qc_json_str.find('{')
                        last_idx = qc_json_str.rfind('}')
                        if start_idx != -1 and last_idx != -1 and last_idx >= start_idx:
                            qc_json_str = qc_json_str[start_idx:last_idx+1]
                            
                        qc_data = json.loads(qc_json_str)
                        qc_item["qc_completeness"] = qc_data.get("completeness_score", "")
                        qc_item["qc_accuracy"] = qc_data.get("scientific_accuracy_score", "")
                        qc_item["qc_pedagogy"] = qc_data.get("pedagogical_adequacy_score", "")
                        qc_item["qc_decision"] = qc_data.get("final_decision", "")
                        qc_item["qc_justification"] = qc_data.get("justification", "")
                        qc_item["qc_revised_alt"] = qc_data.get("revised_alt_text", "")
                        
                    except Exception as e:
                        logger.error(f"QC Validation failed for item on page {absolute_page_num}: {e}")
                        qc_item["qc_completeness"] = "Error"
                        qc_item["qc_accuracy"] = "Error"
                        qc_item["qc_pedagogy"] = "Error"
                        qc_item["qc_decision"] = "Error"
                        qc_item["qc_justification"] = str(e)
                        qc_item["qc_revised_alt"] = ""
                        
                    validated_items.append(qc_item)
                items = validated_items

            logger.info(f"    Found {len(items)} items on page {absolute_page_num}")
            break # Success, exit retry loop
            
        except Exception as e:
            error_msg = str(e)
            if "429" in error_msg or "502" in error_msg or "500" in error_msg or "503" in error_msg or "quota" in error_msg.lower():
                logger.warning(f"Gemini API rate limit/server error on page {absolute_page_num} (Attempt {attempt+1}/{MAX_RETRIES}): {e}")
                if attempt < MAX_RETRIES - 1:
                    sleep_time = (attempt + 1) * 3 # Exponential/Linear backoff: 3s, 6s, 9s, 12s
                    logger.info(f"Waiting {sleep_time} seconds before retrying page {absolute_page_num}...")
                    time.sleep(sleep_time)
                    continue
            
            # If not a retryable error or max retries reached:
            logger.error(f"Gemini Error on page {absolute_page_num} after {attempt + 1} attempts: {e}")
            items.append({
                "page": absolute_page_num,
                "figure_number": "Error",
                "short_alt": "Error processing page",
                "long_alt": str(e),
                "context_type": "Error",
                "domain": "Error"
            })
            break # Exit retry loop on fatal error

    return items, total_in, total_out

def calculate_cost(input_tokens, output_tokens):
    # Gemini 1.5 Flash Pricing (approx)
    # Input: $0.35 / 1M tokens
    # Output: $1.05 / 1M tokens
    cost_in = (input_tokens / 1_000_000) * 0.35
    cost_out = (output_tokens / 1_000_000) * 1.05
    return cost_in + cost_out

def run_batch_processing(batch_id, files_info, run_qc=False):
    """
    files_info: list of (filename, file_path)
    Runs in a background thread.
    """
    logger.info(f"Starting batch {batch_id} with {len(files_info)} files (QC: {run_qc}). DB Type: {DB_TYPE}, SQLite: {IS_SQLITE}")
    
    # Establish new DB connection for this thread
    conn = get_db_connection()
    
    try:
        query_db("UPDATE batches SET status = 'processing' WHERE id = %s", (batch_id,), commit=True, conn=conn)
        
        for job_id, filepath in files_info:
            logger.info(f"Processing job {job_id}: {filepath}")
            
            query_db("UPDATE jobs SET status = 'processing' WHERE id = %s", (job_id,), commit=True, conn=conn)
            
            doc = None
            try:
                # 1. Provide Pages and Process PDF
                if not os.path.exists(filepath):
                    raise FileNotFoundError(f"File not found: {filepath}")
                    
                doc = fitz.open(filepath)
                
                # Render all pages to memory first
                pages_data = []
                for page_index, page in enumerate(doc):
                    absolute_page_num = page_index + 1
                    pix = page.get_pixmap(matrix=fitz.Matrix(2, 2))
                    img_data = pix.tobytes("png")
                    pages_data.append((img_data, absolute_page_num))
                
                # Close main doc after rendering all pages to memory to free file lock
                doc.close()
                doc = None 
                
                all_items = []
                total_in = 0
                total_out = 0
                
                # Parallel processing of individual pages
                from concurrent.futures import as_completed, ThreadPoolExecutor
                with ThreadPoolExecutor(max_workers=5) as executor:
                    future_to_page = {
                        executor.submit(process_single_image, img_data, absolute_page_num, run_qc): absolute_page_num 
                        for img_data, absolute_page_num in pages_data
                    }
                    
                    for future in as_completed(future_to_page):
                        absolute_page_num = future_to_page[future]
                        try:
                            items, i_tok, o_tok = future.result()
                            all_items.extend(items)
                            total_in += i_tok
                            total_out += o_tok
                        except Exception as e:
                            logger.error(f"Page processing failed for page {absolute_page_num}: {e}")

                # 2. Generate Excel
                wb = Workbook()
                ws = wb.active
                ws.title = "Alt Text"
                headers = ["File name", "Figure number", "Page number", "Short alt text", "Long alt text", "Context Type", "Domain"]
                if run_qc:
                    headers.extend(["QC Completeness", "QC Accuracy", "QC Pedagogy", "QC Decision", "QC Justification", "Revised Alt Text"])
                ws.append(headers)
                
                all_items.sort(key=lambda x: x.get("page", 0))
                
                filename = os.path.basename(filepath)
                for item in all_items:
                    row = [
                        filename,
                        item.get("figure_number", "unknown"),
                        item.get("page", "unknown"),
                        clean_alt_text(item.get("short_alt", "")),
                        clean_alt_text(item.get("long_alt", "")),
                        item.get("context_type", "General"),
                        item.get("domain", "General")
                    ]
                    if run_qc:
                        row.extend([
                            item.get("qc_completeness", ""),
                            item.get("qc_accuracy", ""),
                            item.get("qc_pedagogy", ""),
                            item.get("qc_decision", ""),
                            item.get("qc_justification", ""),
                            clean_alt_text(item.get("qc_revised_alt", ""))
                        ])
                    ws.append(row)
                
                out_name = f"{os.path.splitext(filename)[0]}_alt_text.xlsx"
                out_path = os.path.join(OUTPUT_FOLDER, out_name)
                wb.save(out_path)
                
                cost = calculate_cost(total_in, total_out)
                
                query_db("""
                    UPDATE jobs 
                    SET status = 'completed', 
                        output_file = %s, 
                        input_tokens = %s, 
                        output_tokens = %s, 
                        cost = %s 
                    WHERE id = %s
                """, (out_name, total_in, total_out, cost, job_id), commit=True, conn=conn)
                
            except Exception as e:
                logger.error(f"Job {job_id} failed: {e}")
                import traceback
                logger.error(traceback.format_exc())
                if doc: doc.close()
                error_msg = str(e)[:500] 
                query_db("UPDATE jobs SET status = 'failed', error_msg = %s WHERE id = %s", 
                        (error_msg, job_id), commit=True, conn=conn)

        query_db("UPDATE batches SET status = 'completed' WHERE id = %s", (batch_id,), commit=True, conn=conn)
        
    except Exception as e:
        logger.error(f"Batch {batch_id} failed CRITICALLY: {e}")
        import traceback
        logger.error(traceback.format_exc())
        try:
             query_db("UPDATE batches SET status = 'failed' WHERE id = %s", (batch_id,), commit=True, conn=conn)
        except:
             logger.error("Could not update batch status to failed")
    finally:
        if conn: conn.close()


# ---------------- ROUTES ----------------

@app.route("/", methods=["GET"])
@login_required
def index():
    return render_template('dashboard.html', active_page='dashboard')

@app.route("/upload", methods=["GET"])
@login_required
def upload_page():
    return render_template('upload.html', active_page='upload')

@app.route("/batches", methods=["GET"])
@login_required
def batches_page():
    return render_template('batches.html', active_page='batches')

@app.route("/batch/<int:batch_id>", methods=["GET"])
@login_required
def batch_details_page(batch_id):
    return render_template('batch_details.html', active_page='batches', batch_id=batch_id)

@app.route("/download/<path:filename>")
@login_required
def download_file(filename):
    # Security check: ensure filename doesn't contain .. or strictly limit to output folder
    safe_path = os.path.basename(filename)
    return send_file(os.path.join(OUTPUT_FOLDER, safe_path), as_attachment=True)

@app.route('/favicon.ico')
def favicon():
    return '', 204

# API Routes matching frontend/src/api/client.ts

@app.route("/files", methods=["GET"])
@login_required
def files_page():
    files_data = []
    try:
        if os.path.exists(OUTPUT_FOLDER):
            for f in os.listdir(OUTPUT_FOLDER):
                if not f.startswith('.'): # Ignore hidden files
                    path = os.path.join(OUTPUT_FOLDER, f)
                    stats = os.stat(path)
                    files_data.append({
                        'name': f,
                        'size': stats.st_size,
                        'mtime': datetime.datetime.fromtimestamp(stats.st_mtime),
                        'is_xlsx': f.endswith('.xlsx')
                    })
        # Sort by newest first
        files_data.sort(key=lambda x: x['mtime'], reverse=True)
    except Exception as e:
        print(f"Error listing output files: {e}")
        
    return render_template('download.html', active_page='files', files=files_data)

@app.route("/api/queue/batch", methods=["POST"])
def create_batch_route():
    if "files" not in request.files:
        return jsonify({"error": "No files uploaded"}), 400
        
    files = request.files.getlist("files")
    # React sends 'document_type', 'use_markers' (string), 'batch_name', 'run_qc'
    batch_name = request.form.get("batch_name") or f"Batch {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}"
    run_qc = request.form.get("run_qc") == "true"
    
    if not files:
        return jsonify({"error": "No files selected"}), 400

    # Filter for valid files
    valid_files = []
    for f in files:
        if f.filename and (f.filename.lower().endswith('.pdf') or f.filename.lower().endswith('.docx')):
            valid_files.append(f)
            
    if not valid_files:
         return jsonify({"error": "No valid PDF or DOCX files found"}), 400

    # Insert Batch
    batch_id = query_db("INSERT INTO batches (name, status) VALUES (%s, %s) RETURNING id", 
                       (batch_name, 'pending'), commit=True, return_id=True)
    jobs_to_process = []
    
    for file in valid_files:
        path = os.path.join(UPLOAD_FOLDER, file.filename)
        file.save(path)
        
        job_id = query_db("INSERT INTO jobs (batch_id, filename, status) VALUES (%s, %s, %s) RETURNING id", 
                         (batch_id, file.filename, 'pending'), commit=True, return_id=True)
        jobs_to_process.append((job_id, path))
            
    # Start background processing
    thread = threading.Thread(target=run_batch_processing, args=(batch_id, jobs_to_process, run_qc))
    thread.start()
    
    # Return structure matching CreateBatchResponse interface if needed.
    # The frontend expects { batch: { batch_id: ... } } or similar?
    # Looking at client.ts: return response.data.
    # Let's check CreateBatchResponse in types.
    # For now, return a compatible object.
    return jsonify({
        "batch": {
            "batch_id": batch_id,
            "name": batch_name,
            "status": "pending",
            "created_at": datetime.datetime.now().isoformat()
        },
        "success": True
    })

@app.route("/api/queue/batches", methods=["GET"]) 
@app.route("/api/batches", methods=["GET"])
@login_required
def list_batches_route():
    try:
        limit = request.args.get('limit', 50, type=int)
        batches = query_db("""
            SELECT b.id, b.name, b.created_at, b.status,
                   (SELECT COUNT(*) FROM jobs WHERE batch_id = b.id) as total_jobs,
                   (SELECT COUNT(*) FROM jobs WHERE batch_id = b.id AND status = 'completed') as completed_jobs,
                   (SELECT SUM(cost) FROM jobs WHERE batch_id = b.id) as total_cost
            FROM batches b
            ORDER BY b.created_at DESC
            LIMIT %s
        """, (limit,))
        
        result = []
        for b in batches:
            result.append({
                "batch_id": b['id'],
                "name": b['name'],
                "created_at": b['created_at'],
                "status": b['status'],
                "total_jobs": b['total_jobs'],
                "completed_jobs": b['completed_jobs'],
                "cost": { "total_cost": b['total_cost'] or 0.0 }
            })
            
        return jsonify({"batches": result})
    except Exception as e:
        print(f"Error listing batches: {e}")
        return jsonify({"batches": []}) # Return empty list on error to avoid crash

@app.route("/api/queue/batch/<int:batch_id>", methods=["GET"])
@login_required
def get_batch_details(batch_id):
    batch = query_db("SELECT * FROM batches WHERE id = %s", (batch_id,), one=True)
    if not batch:
        return jsonify({"error": "Batch not found"}), 404
        
    jobs = query_db("SELECT * FROM jobs WHERE batch_id = %s", (batch_id,))
    
    jobs_data = []
    for j in jobs:
        jobs_data.append({
            "job_id": j['id'],
            "filename": j['filename'],
            "status": j['status'],
            "cost": { "total": j['cost'] or 0.0 },
            "input_tokens": j['input_tokens'],
            "output_tokens": j['output_tokens'],
            "error_message": j['error_msg'],
            "output_file": j['output_file']
        })
        
    res = query_db("SELECT SUM(cost) as total FROM jobs WHERE batch_id = %s", (batch_id,), one=True)
    total_cost = res['total'] if res and res['total'] else 0.0
    
    return jsonify({
        "batch_id": batch['id'],
        "name": batch['name'],
        "status": batch['status'],
        "created_at": batch['created_at'],
        "jobs": jobs_data,
         "cost": { "total_cost": total_cost }
    })

@app.route("/api/queue/status", methods=["GET"])
@login_required
def get_queue_status_route():
    try:
        pending = query_db("SELECT COUNT(*) as count FROM jobs WHERE status = 'pending'", one=True)['count']
        processing = query_db("SELECT COUNT(*) as count FROM jobs WHERE status = 'processing'", one=True)['count']
        completed = query_db("SELECT COUNT(*) as count FROM jobs WHERE status = 'completed'", one=True)['count']
        failed = query_db("SELECT COUNT(*) as count FROM jobs WHERE status = 'failed'", one=True)['count']
        
        return jsonify({
            "pending": pending,
            "processing": processing,
            "completed": completed,
            "failed": failed,
            "is_processing": processing > 0
        })
    except:
        return jsonify({
            "pending": 0, "processing": 0, "completed": 0, "failed": 0, "is_processing": False
        })

@app.route("/api/queue/stats/tokens", methods=["GET"])
@app.route("/api/tokens/stats", methods=["GET"])
@login_required
def get_token_stats_route():
    stats = query_db("""
        SELECT 
            SUM(input_tokens) as total_input, 
            SUM(output_tokens) as total_output, 
            SUM(cost) as total_cost,
            COUNT(*) as total_jobs
        FROM jobs 
        WHERE status = 'completed'
    """, one=True)
    
    today_stats = query_db("""
        SELECT SUM(cost) as today_cost 
        FROM jobs 
        WHERE status = 'completed' AND date(created_at) = CURRENT_DATE
    """, one=True)

    total_input = stats['total_input'] or 0
    total_output = stats['total_output'] or 0
    total_cost = stats['total_cost'] or 0.0
    total_jobs = stats['total_jobs'] or 0
    today_cost = today_stats['today_cost'] or 0.0 if today_stats else 0.0
    avg_cost = total_cost / total_jobs if total_jobs > 0 else 0
    
    return jsonify({
        "all_time": {
            "total_tokens": total_input + total_output,
            "input_tokens": total_input,
            "output_tokens": total_output,
            "total_jobs": total_jobs,
            "cost": { "total_cost": total_cost }
        },
        "today": {
            "cost": { "total_cost": today_cost }
        },
        "averages": {
            "cost_per_job": avg_cost
        },
        "pricing": {
            "model": MODEL_NAME
        }
    })

if __name__ == "__main__":
    app.run(debug=True, host='0.0.0.0', use_reloader=False)
