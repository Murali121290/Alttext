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
import json
import worker_tasks
from utils.prompt_assets import SYSTEM_PROMPT
from werkzeug.security import generate_password_hash, check_password_hash
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from functools import wraps
import logging
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from utils.security import PasswordValidator, FileValidator, sanitize_filename, check_default_credentials

# ---------------- LOGGING CONFIG ----------------
_stream_handler = logging.StreamHandler()
_stream_handler.stream = open(_stream_handler.stream.fileno(), mode='w', encoding='utf-8', buffering=1)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("alttext_processing.log", encoding='utf-8'),
        _stream_handler
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
MODEL_NAME = "gemini-2.5-pro" 

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

    # Try PostgreSQL — single attempt with a short timeout to avoid blocking startup
    if DB_TYPE == "postgres":
        try:
            conn = psycopg2.connect(DATABASE_URL, connect_timeout=3)
            return conn
        except psycopg2.OperationalError:
            print("\n[WARNING] PostgreSQL connection failed (Docker not running?).")
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
            gpt_input_tokens INTEGER DEFAULT 0,
            gpt_output_tokens INTEGER DEFAULT 0,
            gpt_cost REAL DEFAULT 0.0,
            error_msg TEXT,
            created_at TIMESTAMP {ts_default},
            FOREIGN KEY(batch_id) REFERENCES batches(id)
        )''',
        f'''CREATE TABLE IF NOT EXISTS users (
            id {id_type},
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            role TEXT DEFAULT 'user',
            must_change_password BOOLEAN DEFAULT FALSE,
            last_password_change TIMESTAMP,
            created_at TIMESTAMP {ts_default}
        )'''
    ]

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        for ddl in tables:
            cur.execute(ddl)

        # Add new columns if they don't exist (for existing databases)
        try:
            if IS_SQLITE:
                # SQLite doesn't support IF NOT EXISTS in ALTER TABLE, so check first
                cur.execute("PRAGMA table_info(users)")
                u_cols = [row[1] for row in cur.fetchall()]
                if 'must_change_password' not in u_cols:
                    cur.execute("ALTER TABLE users ADD COLUMN must_change_password BOOLEAN DEFAULT FALSE")
                if 'last_password_change' not in u_cols:
                    cur.execute("ALTER TABLE users ADD COLUMN last_password_change TIMESTAMP")

                cur.execute("PRAGMA table_info(jobs)")
                j_cols = [row[1] for row in cur.fetchall()]
                if 'gpt_input_tokens' not in j_cols:
                    cur.execute("ALTER TABLE jobs ADD COLUMN gpt_input_tokens INTEGER DEFAULT 0")
                if 'gpt_output_tokens' not in j_cols:
                    cur.execute("ALTER TABLE jobs ADD COLUMN gpt_output_tokens INTEGER DEFAULT 0")
                if 'gpt_cost' not in j_cols:
                    cur.execute("ALTER TABLE jobs ADD COLUMN gpt_cost REAL DEFAULT 0.0")

            else:
                # PostgreSQL supports IF NOT EXISTS
                cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS must_change_password BOOLEAN DEFAULT FALSE")
                cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS last_password_change TIMESTAMP")

                cur.execute("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS gpt_input_tokens INTEGER DEFAULT 0")
                cur.execute("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS gpt_output_tokens INTEGER DEFAULT 0")
                cur.execute("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS gpt_cost REAL DEFAULT 0.0")
            conn.commit()
        except Exception as e:
            logger.warning(f"Column migration warning (may be normal if columns exist): {e}")
            conn.rollback()

        # Create indexes for better query performance
        indexes = [
            "CREATE INDEX IF NOT EXISTS idx_jobs_batch_id ON jobs(batch_id)",
            "CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status)",
            "CREATE INDEX IF NOT EXISTS idx_jobs_created_at ON jobs(created_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_batches_created_at ON batches(created_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_batches_status ON batches(status)",
            "CREATE INDEX IF NOT EXISTS idx_users_username ON users(username)",
            "CREATE INDEX IF NOT EXISTS idx_users_role ON users(role)"
        ]

        for index_sql in indexes:
            try:
                cur.execute(index_sql)
                conn.commit()
            except Exception as e:
                logger.warning(f"Index creation warning (may already exist): {e}")
                conn.rollback()

        # Reset any jobs/batches that were left 'processing' due to a crash or corruption
        cur.execute("UPDATE jobs SET status = 'failed', error_msg = 'System crashed or corrupt file prevented completion.' WHERE status = 'processing'")
        cur.execute("UPDATE batches SET status = 'failed' WHERE status = 'processing'")

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
                cur.execute(f"INSERT INTO users (username, password, role, must_change_password) VALUES ({placeholder}, {placeholder}, {placeholder}, {placeholder})",
                              ('admin', enc_pw, 'admin', True))
                conn.commit()
                logger.warning("Default admin account created with password 'admin123' - MUST BE CHANGED ON FIRST LOGIN")
            except (sqlite3.IntegrityError, psycopg2.errors.UniqueViolation, psycopg2.IntegrityError):
                # Another worker likely created the user already
                conn.rollback()
                pass
        else:
            # Mark existing admin with default password to require change
            if IS_SQLITE:
                cur.execute(f"SELECT * FROM users WHERE username = {placeholder}", ('admin',))
                admin = cur.fetchone()
                admin_password = admin[2] if admin else None  # password is column index 2
            else:
                dict_cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
                dict_cur.execute("SELECT * FROM users WHERE username = %s", ('admin',))
                admin = dict_cur.fetchone()
                dict_cur.close()
                admin_password = admin['password'] if admin else None
            if admin and admin_password and check_password_hash(admin_password, 'admin123'):
                cur.execute(f"UPDATE users SET must_change_password = {placeholder} WHERE username = {placeholder}", (True, 'admin'))
                conn.commit()
                logger.warning("Default admin password detected - password change will be required on next login")
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

# Enable template caching in production
if not app.debug:
    app.jinja_env.auto_reload = False
    app.jinja_env.cache_size = 400

# Add cache headers for static files to speed up repeat visits
@app.after_request
def add_cache_headers(response):
    if request.path.startswith('/static/'):
        # Cache static files for 1 hour
        response.cache_control.max_age = 3600
        response.cache_control.public = True
    return response

# ---------------- RATE LIMITING CONFIG ----------------
limiter = Limiter(
    app=app,
    key_func=get_remote_address,
    default_limits=["200 per day", "50 per hour"],
    storage_uri="memory://",  # Use Redis in production: redis://localhost:6379
    strategy="fixed-window"
)

# ---------------- AUTH CONFIG ----------------
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login_page'

class User(UserMixin):
    def __init__(self, id, username, role, must_change_password=False):
        self.id = id
        self.username = username
        self.role = role
        self.must_change_password = must_change_password

@login_manager.user_loader
def load_user(user_id):
    u = query_db("SELECT * FROM users WHERE id = %s", (user_id,), one=True)
    if u:
        must_change = dict(u).get('must_change_password', False)
        # Convert from various possible boolean representations
        if isinstance(must_change, (int, bool)):
            must_change = bool(must_change)
        elif isinstance(must_change, str):
            must_change = must_change.lower() in ('true', '1', 't', 'yes')
        return User(id=u['id'], username=u['username'], role=u['role'], must_change_password=must_change)
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
@limiter.limit("10 per minute")
def login_page():
    if current_user.is_authenticated:
        # Check if user must change password
        if current_user.must_change_password:
            return flask_redirect("/change-password")
        return flask_redirect("/")

    if request.method == "POST":
        username = request.form.get("username")
        password = request.form.get("password")

        # Warn about default credentials
        if check_default_credentials(username, password):
            logger.warning(f"Default credentials used for login attempt: {username}")

        user = query_db("SELECT * FROM users WHERE username = %s", (username,), one=True)

        # Check password
        if user and check_password_hash(user['password'], password):
            must_change = dict(user).get('must_change_password', False)
            if isinstance(must_change, (int, bool)):
                must_change = bool(must_change)
            elif isinstance(must_change, str):
                must_change = must_change.lower() in ('true', '1', 't', 'yes')

            user_obj = User(id=user['id'], username=user['username'], role=user['role'], must_change_password=must_change)
            login_user(user_obj)

            # Redirect to password change if required
            if must_change:
                flash("You must change your password before continuing.", "warning")
                return flask_redirect("/change-password")

            return flask_redirect(request.args.get("next") or "/")
        else:
            logger.warning(f"Failed login attempt for user: {username}")
            return render_template("login.html", error="Invalid credentials")

    return render_template("login.html")

@app.route("/logout")
@login_required
def logout():
    logout_user()
    return flask_redirect("/login")

@app.route("/change-password", methods=["GET", "POST"])
@login_required
def change_password():
    if request.method == "POST":
        current_password = request.form.get("current_password")
        new_password = request.form.get("new_password")
        confirm_password = request.form.get("confirm_password")

        # Verify current password
        user = query_db("SELECT * FROM users WHERE id = %s", (current_user.id,), one=True)
        if not user or not check_password_hash(user['password'], current_password):
            return render_template("change_password.html", error="Current password is incorrect", must_change=current_user.must_change_password)

        # Validate new password
        if new_password != confirm_password:
            return render_template("change_password.html", error="New passwords do not match", must_change=current_user.must_change_password)

        # Check password complexity
        is_valid, error_msg = PasswordValidator.validate(new_password)
        if not is_valid:
            return render_template("change_password.html", error=error_msg, must_change=current_user.must_change_password)

        # Don't allow same password
        if current_password == new_password:
            return render_template("change_password.html", error="New password must be different from current password", must_change=current_user.must_change_password)

        # Update password
        hashed = generate_password_hash(new_password)
        query_db("""
            UPDATE users
            SET password = %s, must_change_password = %s, last_password_change = CURRENT_TIMESTAMP
            WHERE id = %s
        """, (hashed, False, current_user.id), commit=True)

        # Update current user session
        current_user.must_change_password = False

        flash("Password changed successfully.", "success")
        logger.info(f"User {current_user.username} changed their password")
        return flask_redirect("/")

    return render_template("change_password.html", must_change=current_user.must_change_password)

@app.route("/register", methods=["GET", "POST"])
@limiter.limit("5 per hour")
def register_page():
    # Public registration
    if current_user.is_authenticated:
        return flask_redirect("/")
        
    if request.method == "POST":
        username = request.form.get("username")
        password = request.form.get("password")
        confirm_password = request.form.get("confirm_password")

        # Validate username
        if not username or len(username) < 3:
            return render_template("register.html", error="Username must be at least 3 characters long")

        if password != confirm_password:
            return render_template("register.html", error="Passwords do not match")

        # Validate password complexity
        is_valid, error_msg = PasswordValidator.validate(password)
        if not is_valid:
            return render_template("register.html", error=error_msg)

        try:
            # Check if user exists
            if query_db("SELECT id FROM users WHERE username = %s", (username,), one=True):
                 return render_template("register.html", error="Username already exists")

            hashed = generate_password_hash(password)
            query_db("INSERT INTO users (username, password, role, must_change_password, last_password_change) VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP)",
                    (username, hashed, 'user', False), commit=True)

            # Login immediately
            user = query_db("SELECT * FROM users WHERE username = %s", (username,), one=True)
            user_obj = User(id=user['id'], username=user['username'], role=user['role'], must_change_password=False)
            login_user(user_obj)

            logger.info(f"New user registered: {username}")
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
    # Security check: sanitize filename and ensure it's in output folder
    safe_filename = sanitize_filename(filename)
    file_path = os.path.join(OUTPUT_FOLDER, safe_filename)

    # Verify the file exists and is within OUTPUT_FOLDER
    if not os.path.exists(file_path):
        flash("File not found", "error")
        return flask_redirect("/files")

    # Prevent directory traversal
    real_path = os.path.realpath(file_path)
    real_output = os.path.realpath(OUTPUT_FOLDER)
    if not real_path.startswith(real_output):
        logger.warning(f"Attempted directory traversal by {current_user.username}: {filename}")
        flash("Access denied", "error")
        return flask_redirect("/files")

    return send_file(real_path, as_attachment=True)

@app.route('/favicon.ico')
def favicon():
    return '', 204

# API Routes matching frontend/src/api/client.ts

@app.route("/files", methods=["GET"])
@login_required
def files_page():
    files_data = []
    try:
        # Get all completed jobs to map filenames to job IDs
        completed_jobs = query_db("SELECT id, output_file FROM jobs WHERE status = 'completed' AND output_file IS NOT NULL")
        file_to_job = {j['output_file']: j['id'] for j in completed_jobs}

        if os.path.exists(OUTPUT_FOLDER):
            for f in os.listdir(OUTPUT_FOLDER):
                if not f.startswith('.'): # Ignore hidden files
                    path = os.path.join(OUTPUT_FOLDER, f)
                    stats = os.stat(path)
                    files_data.append({
                        'name': f,
                        'size': stats.st_size,
                        'mtime': datetime.datetime.fromtimestamp(stats.st_mtime),
                        'is_xlsx': f.endswith('.xlsx'),
                        'job_id': file_to_job.get(f)  # Attach the job ID if it exists
                    })
        # Sort by newest first
        files_data.sort(key=lambda x: x['mtime'], reverse=True)
    except Exception as e:
        print(f"Error listing output files: {e}")
        
    return render_template('download.html', active_page='files', files=files_data)

@app.route("/api/queue/batch", methods=["POST"])
@limiter.limit("20 per hour")
@login_required
def create_batch_route():
    if "files" not in request.files:
        return jsonify({"error": "No files uploaded"}), 400

    files = request.files.getlist("files")
    batch_name = request.form.get("batch_name") or f"Batch {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}"
    run_gemini = request.form.get("run_gemini", "true").lower() == "true"
    run_gpt = request.form.get("run_gpt", "false").lower() == "true"

    if not files:
        return jsonify({"error": "No files selected"}), 400

    # Validate batch size and count
    is_valid, error_msg = FileValidator.validate_batch(files)
    if not is_valid:
        return jsonify({"error": error_msg}), 400

    # Filter and validate files
    valid_files = []
    validation_errors = []

    for f in files:
        if not f.filename:
            continue

        # Check extension
        filename_lower = f.filename.lower()
        if not (filename_lower.endswith('.pdf') or filename_lower.endswith('.docx') or filename_lower.endswith('.xlsx') or filename_lower.endswith('.xls')):
            validation_errors.append(f"{f.filename}: Invalid file type")
            continue

        # Sanitize filename
        safe_filename = sanitize_filename(f.filename)

        # Save temporarily for validation
        temp_path = os.path.join(UPLOAD_FOLDER, f"temp_{safe_filename}")
        f.save(temp_path)

        # Deep file validation
        is_valid_file, file_error = FileValidator.validate_file(temp_path, f.filename)
        if not is_valid_file:
            os.remove(temp_path)
            validation_errors.append(f"{f.filename}: {file_error}")
            continue

        # Rename to final name
        final_path = os.path.join(UPLOAD_FOLDER, safe_filename)
        # Handle name collisions
        counter = 1
        while os.path.exists(final_path):
            name, ext = os.path.splitext(safe_filename)
            final_path = os.path.join(UPLOAD_FOLDER, f"{name}_{counter}{ext}")
            counter += 1

        os.rename(temp_path, final_path)
        valid_files.append((safe_filename, final_path))

    if not valid_files:
        error_summary = "; ".join(validation_errors) if validation_errors else "No valid PDF, DOCX, or Excel files found"
        return jsonify({"error": error_summary}), 400

    if validation_errors:
        logger.warning(f"File validation errors: {validation_errors}")

    # Insert Batch
    batch_id = query_db("INSERT INTO batches (name, status) VALUES (%s, %s) RETURNING id",
                       (batch_name, 'pending'), commit=True, return_id=True)
    jobs_to_process = []

    for filename, filepath in valid_files:
        job_id = query_db("INSERT INTO jobs (batch_id, filename, status) VALUES (%s, %s, %s) RETURNING id",
                         (batch_id, filename, 'pending'), commit=True, return_id=True)
        jobs_to_process.append((job_id, filepath))
            
    # Delegate background processing to a local thread
    thread = threading.Thread(
        target=worker_tasks.run_batch_processing,
        args=(batch_id, jobs_to_process, run_gemini, run_gpt)
    )
    thread.daemon = True
    thread.start()
    logger.info(f"Started background thread for Batch {batch_id}")
    
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

import zipfile
from utils.pdf_image_extractor import extract_images_to_excel

@app.route("/api/extract_images", methods=["POST"])
@limiter.limit("20 per hour")
@login_required
def extract_images_only_route():
    if "files" not in request.files:
        return jsonify({"error": "No files uploaded"}), 400

    files = request.files.getlist("files")
    if not files or not files[0].filename:
        return jsonify({"error": "No files selected"}), 400

    f = files[0] # We only process the first file for this quick tool
    filename_lower = f.filename.lower()
    
    if not filename_lower.endswith('.pdf'):
        return jsonify({"error": "Only PDF files are supported for Image Extraction"}), 400

    safe_filename = sanitize_filename(f.filename)
    temp_pdf_path = os.path.join(UPLOAD_FOLDER, f"extract_temp_{safe_filename}")
    f.save(temp_pdf_path)

    base_name = os.path.splitext(safe_filename)[0]
    extraction_folder = os.path.join(OUTPUT_FOLDER, f"{base_name}_images")
    excel_name = f"{base_name}_ImageReport.xlsx"

    try:
        # Call the new module
        report_path = extract_images_to_excel(temp_pdf_path, extraction_folder, excel_name)
        
        if not report_path or not os.path.exists(extraction_folder):
             return jsonify({"error": "Extraction failed"}), 500

        # Zip the output folder
        zip_filename = f"{base_name}_ExtractedImages.zip"
        zip_path = os.path.join(OUTPUT_FOLDER, zip_filename)

        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
            for root, dirs, files_in_dir in os.walk(extraction_folder):
                for file in files_in_dir:
                    file_path = os.path.join(root, file)
                    arcname = os.path.relpath(file_path, extraction_folder)
                    zipf.write(file_path, arcname)

        # Clean up temp PDF and raw extraction folder
        if os.path.exists(temp_pdf_path):
            os.remove(temp_pdf_path)
            
        import shutil
        if os.path.exists(extraction_folder):
            shutil.rmtree(extraction_folder)

        # Return the zip file securely
        real_path = os.path.realpath(zip_path)
        return send_file(real_path, as_attachment=True, download_name=zip_filename)

    except Exception as e:
        logger.error(f"Image extraction route failed: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/api/queue/batches", methods=["GET"]) 
@app.route("/api/batches", methods=["GET"])
@limiter.exempt
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
@limiter.exempt
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
@limiter.exempt
@login_required
def get_token_stats_route():
    stats = query_db("""
        SELECT 
            SUM(input_tokens) as total_gemini_in, 
            SUM(output_tokens) as total_gemini_out, 
            SUM(cost) as total_gemini_cost,
            SUM(gpt_input_tokens) as total_gpt_in,
            SUM(gpt_output_tokens) as total_gpt_out,
            SUM(gpt_cost) as total_gpt_cost,
            COUNT(*) as total_jobs
        FROM jobs 
        WHERE status = 'completed'
    """, one=True)
    
    today_stats = query_db("""
        SELECT 
            SUM(cost) as gemini_today_cost,
            SUM(gpt_cost) as gpt_today_cost
        FROM jobs 
        WHERE status = 'completed' AND date(created_at) = CURRENT_DATE
    """, one=True)

    gem_in = stats['total_gemini_in'] or 0
    gem_out = stats['total_gemini_out'] or 0
    gem_cost = stats['total_gemini_cost'] or 0.0

    gpt_in = stats['total_gpt_in'] or 0
    gpt_out = stats['total_gpt_out'] or 0
    gpt_cost = stats['total_gpt_cost'] or 0.0

    total_jobs = stats['total_jobs'] or 0
    
    gem_today = today_stats['gemini_today_cost'] or 0.0 if today_stats else 0.0
    gpt_today = today_stats['gpt_today_cost'] or 0.0 if today_stats else 0.0

    gem_avg = gem_cost / total_jobs if total_jobs > 0 else 0
    gpt_avg = gpt_cost / total_jobs if total_jobs > 0 else 0
    
    return jsonify({
        "gemini": {
            "all_time": {
                "total_tokens": gem_in + gem_out,
                "input_tokens": gem_in,
                "output_tokens": gem_out,
                "total_jobs": total_jobs,
                "cost": { "total_cost": gem_cost }
            },
            "today": { "cost": { "total_cost": gem_today } },
            "averages": { "cost_per_job": gem_avg },
            "pricing": { "model": MODEL_NAME }
        },
        "gpt": {
            "all_time": {
                "total_tokens": gpt_in + gpt_out,
                "input_tokens": gpt_in,
                "output_tokens": gpt_out,
                "total_jobs": total_jobs,
                "cost": { "total_cost": gpt_cost }
            },
            "today": { "cost": { "total_cost": gpt_today } },
            "averages": { "cost_per_job": gpt_avg },
            "pricing": { "model": "gpt-4o" }
        }
    })

# ============================================================
# MARKUP TOOL — Additive new module.
# Existing routes, logic, and database code are untouched.
# ============================================================
import uuid as _uuid

MARKUP_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "markup_sessions")
os.makedirs(MARKUP_FOLDER, exist_ok=True)


@app.route("/markup")
@login_required
def markup_page():
    return render_template("mark.html", title="Markup Tool", active_page="markup")


@app.route("/api/markup/upload", methods=["POST"])
@login_required
def markup_upload():
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"error": "No file uploaded"}), 400
    if not f.filename.lower().endswith(".pdf"):
        return jsonify({"error": "Only PDF files are supported"}), 400

    session_id = str(_uuid.uuid4())
    session_dir = os.path.join(MARKUP_FOLDER, session_id)
    os.makedirs(session_dir, exist_ok=True)

    pdf_path = os.path.join(session_dir, "source.pdf")
    f.save(pdf_path)

    try:
        doc = fitz.open(pdf_path)
        page_count = len(doc)
        pages_meta = []
        # PDF.js renders pages client-side — no server-side PNG conversion needed
        for i in range(len(doc)):
            page = doc[i]
            pages_meta.append({
                "page": i,
                "width_pt": page.rect.width,
                "height_pt": page.rect.height,
            })
        # --- Detect existing PDF annotations and convert to pre-populated regions ---
        _MARKUP_ANNOT_TYPES = {
            0,   # Text (sticky note / comment)
            2,   # FreeText (callout / text box)
            4,   # Square / Rectangle
            5,   # Circle
            8,   # Highlight
            9,   # Underline
            10,  # Squiggly
            11,  # StrikeOut
            13,  # Stamp
            15,  # Ink (freehand)
            20,  # Polygon
        }
        pre_regions = []
        annot_counter = 0
        for i in range(len(doc)):
            page = doc[i]
            pw = page.rect.width
            ph = page.rect.height
            for annot in page.annots():
                if annot.type[0] not in _MARKUP_ANNOT_TYPES:
                    continue
                r = annot.rect
                # Skip degenerate / invisible rects (< 0.3 % of page in either dimension)
                if pw == 0 or ph == 0:
                    continue
                if (r.width / pw) < 0.003 or (r.height / ph) < 0.003:
                    continue
                label = (annot.info.get("content") or annot.info.get("title") or "").strip()
                annot_counter += 1
                pre_regions.append({
                    "id": annot_counter,
                    "page": i,
                    "x0_pct": r.x0 / pw,
                    "y0_pct": r.y0 / ph,
                    "x1_pct": r.x1 / pw,
                    "y1_pct": r.y1 / ph,
                    "label": label,
                })

        doc.close()
    except Exception as e:
        logger.error(f"Markup upload failed: {e}")
        return jsonify({"error": str(e)}), 500

    return jsonify({
        "session_id": session_id,
        "page_count": page_count,
        "pages": pages_meta,
        "pre_regions": pre_regions,
    })


@app.route("/api/markup/pdf/<session_id>")
@login_required
def markup_serve_pdf(session_id):
    """Serve the raw PDF so PDF.js can render it client-side."""
    if not re.match(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', session_id):
        return jsonify({"error": "Invalid session"}), 400
    pdf_path = os.path.join(MARKUP_FOLDER, session_id, "source.pdf")
    real_path = os.path.realpath(pdf_path)
    if not real_path.startswith(os.path.realpath(MARKUP_FOLDER)):
        return jsonify({"error": "Access denied"}), 403
    if not os.path.exists(real_path):
        return jsonify({"error": "Not found"}), 404
    return send_file(real_path, mimetype="application/pdf")


@app.route("/api/markup/generate", methods=["POST"])
@login_required
def markup_generate():
    data = request.get_json()
    if not data:
        return jsonify({"error": "No data provided"}), 400

    session_id = data.get("session_id", "")
    regions = data.get("regions", [])

    if not re.match(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', session_id):
        return jsonify({"error": "Invalid session"}), 400
    if not regions:
        return jsonify({"error": "No regions provided"}), 400

    pdf_path = os.path.join(MARKUP_FOLDER, session_id, "source.pdf")
    real_pdf = os.path.realpath(pdf_path)
    if not real_pdf.startswith(os.path.realpath(MARKUP_FOLDER)):
        return jsonify({"error": "Access denied"}), 403
    if not os.path.exists(real_pdf):
        return jsonify({"error": "Session not found — please re-upload the PDF"}), 404

    try:
        from utils.markup_processor import process_markup_regions, write_markup_excel
        results = process_markup_regions(real_pdf, regions)

        pdf_filename = os.path.basename(real_pdf)
        output_filename = f"markup_{session_id[:8]}.xlsx"
        output_path = os.path.join(OUTPUT_FOLDER, output_filename)
        write_markup_excel(results, output_path, pdf_filename=pdf_filename)

        return jsonify({
            "results": results,
            "download_url": f"/download/{output_filename}"
        })
    except Exception as e:
        logger.error(f"Markup generate failed: {e}")
        return jsonify({"error": str(e)}), 500


_UUID_RE = r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'


@app.route("/api/markup/generate_region", methods=["POST"])
@login_required
def markup_generate_region():
    """Generate alt text for a single drawn region (called automatically on draw/resize/drag)."""
    data = request.get_json()
    if not data:
        return jsonify({"error": "No data provided"}), 400

    session_id = data.get("session_id", "")
    region = data.get("region", {})

    if not re.match(_UUID_RE, session_id):
        return jsonify({"error": "Invalid session"}), 400
    if not region:
        return jsonify({"error": "No region provided"}), 400

    pdf_path = os.path.realpath(os.path.join(MARKUP_FOLDER, session_id, "source.pdf"))
    if not pdf_path.startswith(os.path.realpath(MARKUP_FOLDER)):
        return jsonify({"error": "Access denied"}), 403
    if not os.path.exists(pdf_path):
        return jsonify({"error": "Session not found — please re-upload the PDF"}), 404

    try:
        from utils.markup_processor import process_markup_regions
        results = process_markup_regions(pdf_path, [region])
        return jsonify({"result": results[0]})
    except Exception as e:
        logger.error(f"Markup generate_region failed: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/markup/export", methods=["POST"])
@login_required
def markup_export():
    """Write the current frontend results to Excel and return a download URL."""
    data = request.get_json()
    if not data:
        return jsonify({"error": "No data provided"}), 400

    session_id = data.get("session_id", "")
    results = data.get("results", [])
    pdf_filename = data.get("pdf_filename", "markup.pdf")

    if not re.match(_UUID_RE, session_id):
        return jsonify({"error": "Invalid session"}), 400
    if not results:
        return jsonify({"error": "No results to export"}), 400

    try:
        from utils.markup_processor import write_markup_excel
        output_filename = f"markup_{session_id[:8]}.xlsx"
        output_path = os.path.join(OUTPUT_FOLDER, output_filename)
        write_markup_excel(results, output_path, pdf_filename=pdf_filename)
        return jsonify({"download_url": f"/download/{output_filename}"})
    except Exception as e:
        logger.error(f"Markup export failed: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/markup/refine_region", methods=["POST"])
@login_required
def markup_refine_region():
    """Refine existing alt text for a region based on a user instruction."""
    data = request.get_json()
    if not data:
        return jsonify({"error": "No data provided"}), 400

    session_id = data.get("session_id", "")
    region = data.get("region", {})
    previous_short = data.get("previous_short", "")
    previous_long = data.get("previous_long", "")
    prompt = data.get("prompt", "").strip()

    if not prompt:
        return jsonify({"error": "Refine prompt is required"}), 400
    if not re.match(_UUID_RE, session_id):
        return jsonify({"error": "Invalid session"}), 400
    if not region:
        return jsonify({"error": "No region provided"}), 400

    pdf_path = os.path.realpath(os.path.join(MARKUP_FOLDER, session_id, "source.pdf"))
    if not pdf_path.startswith(os.path.realpath(MARKUP_FOLDER)):
        return jsonify({"error": "Access denied"}), 403
    if not os.path.exists(pdf_path):
        return jsonify({"error": "Session not found — please re-upload the PDF"}), 404

    try:
        from utils.markup_processor import crop_region_to_png, refine_markup_region
        png_bytes = crop_region_to_png(
            pdf_path,
            int(region["page"]),
            float(region["x0_pct"]), float(region["y0_pct"]),
            float(region["x1_pct"]), float(region["y1_pct"]),
        )
        result = refine_markup_region(png_bytes, previous_short, previous_long, prompt)
        return jsonify(result)
    except Exception as e:
        logger.error(f"Markup refine_region failed: {e}")
        return jsonify({"error": str(e)}), 500



# ---------------- REVIEW TOOL ROUTES ----------------

@app.route("/review/<int:job_id>")
@login_required
def review_page(job_id):
    job = query_db("SELECT * FROM jobs WHERE id = %s", (job_id,), one=True)
    if not job:
        flash("Job not found", "error")
        return flask_redirect("/batches")
        
    return render_template("review.html", title="Review Tool", active_page="batches", job_id=job_id, pdf_filename=job['filename'])

import base64
import openpyxl

@app.route("/api/job/<int:job_id>/data", methods=["GET"])
@login_required
def get_job_review_data(job_id):
    job = query_db("SELECT * FROM jobs WHERE id = %s", (job_id,), one=True)
    if not job or not job['output_file']:
        return jsonify({"error": "Job not found or no output file"}), 404

    excel_path = os.path.join(OUTPUT_FOLDER, job['output_file'])
    if not os.path.exists(excel_path):
        return jsonify({"error": "Output file not found"}), 404

    try:
        wb = openpyxl.load_workbook(excel_path, data_only=False)
        ws = wb.active
        
        images_by_row = {}
        if hasattr(ws, '_images'):
            for img in ws._images:
                if hasattr(img, 'anchor') and hasattr(img.anchor, '_from'):
                    row_idx = img.anchor._from.row
                    # Extract bytes
                    try:
                        img_bytes = img._data()
                        if callable(img_bytes):
                            img_bytes = img_bytes()
                        images_by_row[row_idx + 1] = base64.b64encode(img_bytes).decode('utf-8')
                    except Exception as e:
                        logger.error(f"Failed to read image on row {row_idx+1}: {e}")

        results = []
        id_counter = 1
        for row_idx, row in enumerate(ws.iter_rows(min_row=2, values_only=True), start=2):
            if not row[0]: continue
            
            try:
                page_val = int(row[2]) - 1
            except:
                page_val = 0
                
            results.append({
                "id": id_counter,
                "page": page_val,
                "label": str(row[1]) if row[1] else f"Figure {id_counter}",
                "short_alt": str(row[4]) if row[4] else "",
                "long_alt": str(row[5]) if row[5] else "",
                "content_type": str(row[8]) if len(row) > 8 and row[8] else "unknown",
                "domain": str(row[9]) if len(row) > 9 and row[9] else "Education",
                "crop_png_b64": images_by_row.get(row_idx, None),
                "isUserDrawn": False
            })
            id_counter += 1
            
        return jsonify({
            "job_id": job_id,
            "pdf_filename": job['filename'],
            "results": results
        })
    except Exception as e:
        logger.error(f"Error parsing job data: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/api/job/<int:job_id>/pdf", methods=["GET"])
@login_required
def serve_job_pdf(job_id):
    job = query_db("SELECT * FROM jobs WHERE id = %s", (job_id,), one=True)
    if not job:
        return jsonify({"error": "Job not found"}), 404
        
    if not job['filename'].lower().endswith('.pdf'):
        return jsonify({"error": "no_pdf"}), 404

    pdf_path = os.path.join(UPLOAD_FOLDER, job['filename'])
    if not os.path.exists(pdf_path):
        return jsonify({"error": "PDF not found on server"}), 404

    return send_file(pdf_path, mimetype="application/pdf")

@app.route("/api/job/<int:job_id>/update", methods=["POST"])
@login_required
def update_job_review_data(job_id):
    data = request.get_json()
    results = data.get("results", [])
    
    job = query_db("SELECT * FROM jobs WHERE id = %s", (job_id,), one=True)
    if not job or not job['output_file']:
        return jsonify({"error": "Job not found"}), 404
        
    output_path = os.path.join(OUTPUT_FOLDER, job['output_file'])
    pdf_filename = job['filename']
    
    try:
        from utils.markup_processor import write_markup_excel
        write_markup_excel(results, output_path, pdf_filename=pdf_filename)
        return jsonify({"success": True, "download_url": f"/download/{job['output_file']}"})
    except Exception as e:
        logger.error(f"Failed to save reviewed Excel for job {job_id}: {e}")
        return jsonify({"error": str(e)}), 500



@app.route("/api/job/<int:job_id>/generate_region", methods=["POST"])
@login_required
def job_generate_region(job_id):
    data = request.get_json()
    if not data or "region" not in data:
        return jsonify({"error": "No region provided"}), 400

    region = data.get("region", {})
    job = query_db("SELECT * FROM jobs WHERE id = %s", (job_id,), one=True)
    if not job:
        return jsonify({"error": "Job not found"}), 404

    pdf_path = os.path.join(UPLOAD_FOLDER, job['filename'])
    if not os.path.exists(pdf_path):
        return jsonify({"error": "PDF not found"}), 404

    try:
        from utils.markup_processor import process_markup_regions
        results = process_markup_regions(pdf_path, [region])
        return jsonify({"result": results[0]})
    except Exception as e:
        logger.error(f"Job {job_id} generate_region failed: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/api/job/<int:job_id>/refine_region", methods=["POST"])
@login_required
def job_refine_region(job_id):
    data = request.get_json()
    if not data or "region" not in data:
        return jsonify({"error": "No region provided"}), 400

    region = data.get("region", {})
    previous_short = data.get("previous_short", "")
    previous_long = data.get("previous_long", "")
    prompt = data.get("prompt", "").strip()

    if not prompt:
        return jsonify({"error": "Refine prompt is required"}), 400

    job = query_db("SELECT * FROM jobs WHERE id = %s", (job_id,), one=True)
    if not job:
        return jsonify({"error": "Job not found"}), 404

    pdf_path = os.path.join(UPLOAD_FOLDER, job['filename'])
    if not os.path.exists(pdf_path):
        return jsonify({"error": "PDF not found"}), 404

    try:
        from utils.markup_processor import crop_region_to_png, refine_markup_region
        png_bytes = crop_region_to_png(
            pdf_path,
            int(region["page"]),
            float(region["x0_pct"]), float(region["y0_pct"]),
            float(region["x1_pct"]), float(region["y1_pct"]),
        )
        result = refine_markup_region(png_bytes, previous_short, previous_long, prompt)
        return jsonify(result)
    except Exception as e:
        logger.error(f"Job {job_id} refine_region failed: {e}")
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run(debug=True, host='0.0.0.0', use_reloader=False)
