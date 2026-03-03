import os
import fitz  # PyMuPDF
import io
import sqlite3
import psycopg2
import psycopg2.extras
import datetime
import time
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from PIL import Image
from openpyxl import Workbook
import google.genai as genai
from utils.prompt_assets import SYSTEM_PROMPT
from utils.qc_prompt import QC_VALIDATION_PROMPT
from dotenv import load_dotenv
import logging

# ---------------- LOGGING CONFIG ----------------
_stream_handler = logging.StreamHandler()
_stream_handler.stream = open(_stream_handler.stream.fileno(), mode='w', encoding='utf-8', buffering=1)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(process)d - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("alttext_worker.log", encoding='utf-8'),
        _stream_handler
    ]
)
logger = logging.getLogger(__name__)

# ---------------- CONFIG ----------------
load_dotenv()

MODEL_NAME = "gemini-3-flash-preview"
UPLOAD_FOLDER = "uploads"
OUTPUT_FOLDER = "outputs"
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/alttext")

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(OUTPUT_FOLDER, exist_ok=True)

# ---- OPTIMIZATION 1: Single shared Gemini client (no per-page instantiation) ----
_gemini_client = None

def get_gemini_client():
    global _gemini_client
    if _gemini_client is None:
        if not GEMINI_API_KEY:
            raise ValueError("GEMINI_API_KEY not set")
        _gemini_client = genai.Client(api_key=GEMINI_API_KEY)
    return _gemini_client

# ---- OPTIMIZATION 2: Tune concurrency via env vars ----
# For Gemini Flash, 15-20 concurrent requests is safe on a standard quota.
# Raise MAX_WORKERS if you have a higher quota tier.
MAX_WORKERS = int(os.getenv("PDF_MAX_WORKERS", "15"))
# Render resolution: 1.5 is good quality/speed balance. Lower to 1.2 for faster renders.
RENDER_SCALE = float(os.getenv("PDF_RENDER_SCALE", "1.5"))

# ---------------- DATABASE ABSTRACTION ----------------
DB_TYPE = "postgres"
IS_SQLITE = False

def get_db_connection():
    global DB_TYPE, IS_SQLITE
    
    if DB_TYPE == "postgres":
        for attempt in range(5):
            try:
                conn = psycopg2.connect(DATABASE_URL)
                return conn
            except psycopg2.OperationalError:
                if attempt < 4:
                    time.sleep(2)
                    continue
                logger.warning("PostgreSQL connection failed in worker. Falling back to SQLite.")
                DB_TYPE = "sqlite"
                IS_SQLITE = True
            
    conn = sqlite3.connect("alttext.db")
    conn.row_factory = sqlite3.Row
    return conn

def query_db(query, args=(), commit=False, conn=None):
    close_after = False
    if conn is None:
        conn = get_db_connection()
        close_after = True
    
    if IS_SQLITE:
        query = query.replace('%s', '?')
        query = query.replace('RETURNING id', '') 
        query = query.replace('CURRENT_DATE', "date('now')")
    
    if IS_SQLITE:
        cur = conn.cursor()
    else:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        
    try:
        cur.execute(query, args)
        if commit:
            conn.commit()
        cur.close()
    except Exception as e:
        logger.error(f"Worker Query Failed: {query} | Args: {args} | Error: {e}")
        raise e
    finally:
        if close_after:
            conn.close()

# ---------------- CORE LOGIC ----------------
def apply_json_rules_to_alt_text(text):
    if not text:
        return ""
    try:
        rules_path = os.path.join(os.path.dirname(__file__), 'utils', 'alt_text_rules.json')
        if os.path.exists(rules_path):
            with open(rules_path, 'r', encoding='utf-8') as f:
                rules_data = json.load(f)
            
            for rule_name, rule_data in rules_data.get("alt_text_validation_rules", {}).items():
                action = rule_data.get("auto_fix_action")
                if action in ["REMOVE_PHRASE", "REMOVE_WORD"]:
                    for phrase in rule_data.get("words", []):
                        pattern = r'(?i)\b' + re.escape(phrase) + r'\b\s*'
                        text = re.sub(pattern, '', text).strip()
    except Exception as e:
        logger.error(f"Error applying alt text rules programmatically: {e}")
         
    return text

def clean_alt_text(text):
    if not text:
        return ""
        
    # Remove hidden control characters that crash openpyxl
    text = re.sub(r'[\x00-\x08\x0b-\x0c\x0e-\x1f]', '', text)
    
    # Prevent Excel Formula Injection corruption (e.g. text starting with =, +, -)
    text = text.lstrip('=+-@')
    
    text = text.strip()
    lower_text = text.lower()
    if lower_text.startswith("a "):
        text = text[2:].strip()
    elif lower_text.startswith("an "):
        text = text[3:].strip()
    elif lower_text.startswith("the "):
        text = text[4:].strip()
        
    text = re.sub(r'^(Figure|Fig\.?)\s*\d+[:.]\s*', '', text, flags=re.IGNORECASE)
    return text

# ---- OPTIMIZATION 3: Render all pages upfront in a single fast pass ----
def render_pages(doc):
    """
    Renders all pages with images/drawings to PNG bytes.
    Returns list of (img_bytes, page_num_1based).
    This is CPU-bound and fast — done once before any API calls.
    """
    pages_data = []
    total_pages = len(doc)
    matrix = fitz.Matrix(RENDER_SCALE, RENDER_SCALE)

    for page_index in range(total_pages):
        page = doc[page_index]
        absolute_page_num = page_index + 1

        # Skip pages with no visual content
        if not page.get_images() and not page.get_drawings():
            logger.info(f"Skipping page {absolute_page_num} (no images/drawings)")
            continue

        pix = page.get_pixmap(matrix=matrix)
        img_data = pix.tobytes("png")
        pages_data.append((img_data, absolute_page_num))

    logger.info(f"Rendered {len(pages_data)}/{total_pages} pages with visual content")
    return pages_data


def process_single_image(img_data, absolute_page_num, run_qc=False, retry_attempt=0):
    """Process one page image through Gemini. Uses shared client.

    Args:
        img_data: PNG image bytes
        absolute_page_num: Page number (1-based)
        run_qc: Whether to run QC validation
        retry_attempt: Current retry attempt number (0 = first attempt)
    """
    logger.info(f"  Processing Page {absolute_page_num}... (QC: {run_qc}, Retry: {retry_attempt})")
    items = []
    total_in = 0
    total_out = 0

    MAX_RETRIES = 5
    for attempt in range(MAX_RETRIES):
        try:
            image = Image.open(io.BytesIO(img_data))
            if image.mode != "RGB":
                image = image.convert("RGB")
                
            if image.width > 3072 or image.height > 3072:
                image.thumbnail((3072, 3072))
            
            # ---- OPTIMIZATION 1 applied: reuse shared client ----
            client = get_gemini_client()

            # Enhance prompt for retry attempts with more explicit instructions
            context_prompt = f"This is Page {absolute_page_num} of the document.\n\n"
            if retry_attempt > 0:
                context_prompt += """
⚠️ RETRY ATTEMPT {retry_attempt} - CRITICAL INSTRUCTIONS:
Your previous attempt was rejected for being INCOMPLETE. This page likely contains MULTIPLE sections of instructional content.

BEFORE generating alt text, visually scan and COUNT:
1. How many distinct sections/headings are on this page?
2. How many worked examples or problem solutions are shown?
3. How many mathematical equations or formulas are present?
4. How many paragraphs of explanatory text exist?

Your alt text MUST include ALL of the above. If you see 3 worked examples, include all 3. If you see 5 equations, include all 5.

SELF-CHECK: After writing your alt text, verify you captured 100% of the instructional content.

""".format(retry_attempt=retry_attempt)

            context_prompt += SYSTEM_PROMPT
            
            response = client.models.generate_content(
                model=MODEL_NAME,
                contents=[context_prompt, image]
            )
            
            if response.usage_metadata:
                total_in += (response.usage_metadata.prompt_token_count or 0)
                total_out += (response.usage_metadata.candidates_token_count or 0)
            
            text_resp = response.text.strip()
            json_str = text_resp
            
            code_block = re.search(r'```(?:json)?\s*(.*?)```', text_resp, re.DOTALL)
            if code_block:
                json_str = code_block.group(1).strip()
            else:
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

            try:
                data = json.loads(json_str)
            except json.JSONDecodeError:
                logger.error(f"JSON Decode Error on page {absolute_page_num}. Extracted: {json_str[:100]}... Raw: {text_resp[:100]}...")
                data = []

            if isinstance(data, dict):
                data = [data]
            
            for item in data:
                items.append({
                    "page": absolute_page_num,
                    "figure_number": item.get("figure_number", "Unknown"),
                    "short_alt": item.get("short_alt", ""),
                    "long_alt": item.get("long_alt", ""),
                    "context_type": item.get("context_type", ""),
                    "domain": item.get("domain", "")
                })

            if run_qc:
                validated_items = []
                for item in items:
                    qc_item = dict(item)
                    if not item.get("long_alt"):
                        validated_items.append(qc_item)
                        continue
                        
                    try:
                        qc_prompt_text = QC_VALIDATION_PROMPT.format(
                            domain=item.get("domain", "General"),
                            context_type=item.get("context_type", "General"),
                            alt_text=item.get("long_alt")
                        )
                        qc_response = client.models.generate_content(
                            model=MODEL_NAME,
                            contents=[qc_prompt_text, image]
                        )
                        
                        if qc_response.usage_metadata:
                            total_in += (qc_response.usage_metadata.prompt_token_count or 0)
                            total_out += (qc_response.usage_metadata.candidates_token_count or 0)
                            
                        qc_text = qc_response.text.strip()
                        code_block = re.search(r'```(?:json)?\s*(.*?)```', qc_text, re.DOTALL)
                        qc_json_str = code_block.group(1).strip() if code_block else qc_text
                        
                        start_idx = qc_json_str.find('{')
                        last_idx = qc_json_str.rfind('}')
                        if start_idx != -1 and last_idx != -1 and last_idx >= start_idx:
                            qc_json_str = qc_json_str[start_idx:last_idx+1]
                            
                        try:
                            qc_data = json.loads(qc_json_str)
                        except json.JSONDecodeError as jde:
                            logger.warning(f"QC JSON Decode Error on page {absolute_page_num}: {jde}. Attempting basic cleanup...")
                            clean_str = qc_json_str.replace("'", '"')
                            clean_str = re.sub(r',\s*\}', '}', clean_str)
                            try:
                                qc_data = json.loads(clean_str)
                            except json.JSONDecodeError:
                                logger.error(f"Secondary QC JSON Decode Error on page {absolute_page_num}")
                                raise Exception(f"Failed to parse QC JSON")
                            
                        qc_item["qc_completeness"] = qc_data.get("completeness_score", "")
                        qc_item["qc_accuracy"] = qc_data.get("scientific_accuracy_score", "")
                        qc_item["qc_pedagogy"] = qc_data.get("pedagogical_adequacy_score", "")
                        qc_item["qc_decision"] = qc_data.get("final_decision", "")
                        qc_item["qc_justification"] = qc_data.get("justification", "")
                        raw_revised = qc_data.get("revised_alt_text", "")
                        qc_item["qc_revised_alt"] = apply_json_rules_to_alt_text(raw_revised)

                        # Check if QC indicates full rewrite needed and we haven't exceeded retries
                        qc_decision = qc_data.get("final_decision", "")
                        if "❌" in qc_decision or "Needs full rewrite" in qc_decision:
                            if retry_attempt < 2:  # Allow up to 2 retries for QC failures
                                logger.warning(f"Page {absolute_page_num} QC failed with decision: {qc_decision}. Triggering retry {retry_attempt + 1}/2")
                                # Raise an exception to trigger retry with enhanced prompt
                                raise Exception(f"QC validation failed: {qc_decision}. {qc_data.get('justification', '')}")
                        
                    except Exception as e:
                        # Re-raise QC validation failures so they propagate to
                        # process_single_image_with_retry for a proper retry.
                        if "QC validation failed" in str(e):
                            raise
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
            break
            
        except Exception as e:
            error_msg = str(e)
            # QC validation failures must propagate to process_single_image_with_retry
            if "QC validation failed" in error_msg:
                raise
            if "429" in error_msg or "502" in error_msg or "500" in error_msg or "503" in error_msg or "quota" in error_msg.lower():
                logger.warning(f"Gemini API error on page {absolute_page_num} (Attempt {attempt+1}/{MAX_RETRIES}): {e}")
                if attempt < MAX_RETRIES - 1:
                    # ---- OPTIMIZATION 4: Exponential backoff with jitter for rate limits ----
                    sleep_time = (2 ** attempt) + (attempt * 0.5)
                    logger.info(f"    Rate limit hit — sleeping {sleep_time:.1f}s before retry...")
                    time.sleep(sleep_time)
                    continue
            
            logger.error(f"Gemini Error on page {absolute_page_num} after {attempt + 1} attempts: {e}")
            items.append({
                "page": absolute_page_num,
                "figure_number": "Error",
                "short_alt": "Error processing page",
                "long_alt": str(e),
                "context_type": "Error",
                "domain": "Error"
            })
            break

    return items, total_in, total_out

def process_single_image_with_retry(img_data, absolute_page_num, run_qc=False):
    """
    Wrapper for process_single_image that handles QC-driven retries.

    If QC validation fails with "Needs full rewrite", this will retry
    with enhanced prompts up to 2 times.
    """
    MAX_QC_RETRIES = 2

    for retry_attempt in range(MAX_QC_RETRIES + 1):
        try:
            items, total_in, total_out = process_single_image(
                img_data,
                absolute_page_num,
                run_qc,
                retry_attempt
            )

            # Success! Return the results
            if retry_attempt > 0:
                logger.info(f"Page {absolute_page_num} succeeded after {retry_attempt} retry attempt(s)")
            return items, total_in, total_out

        except Exception as e:
            error_msg = str(e)

            # Check if this is a QC failure that should trigger retry
            if "QC validation failed" in error_msg and retry_attempt < MAX_QC_RETRIES:
                logger.warning(f"Page {absolute_page_num} QC failed (attempt {retry_attempt + 1}/{MAX_QC_RETRIES + 1}). Retrying with enhanced prompt...")
                continue  # Retry with next attempt
            else:
                # Either not a QC error, or we've exhausted retries
                if retry_attempt >= MAX_QC_RETRIES:
                    logger.error(f"Page {absolute_page_num} failed QC after {MAX_QC_RETRIES + 1} attempts. Using last attempt's results.")
                # Re-raise to be handled by caller
                raise

def calculate_cost(input_tokens, output_tokens):
    cost_in = (input_tokens / 1_000_000) * 0.35
    cost_out = (output_tokens / 1_000_000) * 1.05
    return cost_in + cost_out

def run_batch_processing(batch_id, files_info, run_qc=False):
    """
    files_info: list of (job_id, file_path)
    Key optimizations vs original:
      1. Single shared Gemini client
      2. All pages rendered upfront before any API calls
      3. No chunking — all pages submitted to thread pool at once
      4. MAX_WORKERS raised to 15 (tune via PDF_MAX_WORKERS env var)
      5. Exponential backoff on rate limit errors
    """
    logger.info(f"Worker started batch {batch_id} with {len(files_info)} files (QC: {run_qc}). MAX_WORKERS={MAX_WORKERS}")
    conn = get_db_connection()
    
    try:
        query_db("UPDATE batches SET status = 'processing' WHERE id = %s", (batch_id,), commit=True, conn=conn)
        
        for job_id, filepath in files_info:
            logger.info(f"Worker processing job {job_id}: {filepath}")
            query_db("UPDATE jobs SET status = 'processing' WHERE id = %s", (job_id,), commit=True, conn=conn)
            
            doc = None
            try:
                if not os.path.exists(filepath):
                    raise FileNotFoundError(f"File not found: {filepath}")
                    
                doc = fitz.open(filepath)
                total_pages = len(doc)
                logger.info(f"Job {job_id}: PDF has {total_pages} pages")

                # ---- OPTIMIZATION 3: Render ALL pages first (fast CPU pass) ----
                logger.info(f"Job {job_id}: Rendering pages...")
                pages_data = render_pages(doc)
                doc.close()
                doc = None
                logger.info(f"Job {job_id}: Rendering done. Submitting {len(pages_data)} pages to {MAX_WORKERS} workers...")

                all_items = []
                total_in = 0
                total_out = 0

                # ---- OPTIMIZATION 2+3: Submit ALL pages at once, no chunking overhead ----
                # Use retry wrapper when QC is enabled
                process_func = process_single_image_with_retry if run_qc else process_single_image
                with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
                    future_to_page = {
                        executor.submit(process_func, img_data, absolute_page_num, run_qc): absolute_page_num
                        for img_data, absolute_page_num in pages_data
                    }
                    
                    completed_count = 0
                    for future in as_completed(future_to_page):
                        absolute_page_num = future_to_page[future]
                        completed_count += 1
                        try:
                            items, i_tok, o_tok = future.result()
                            all_items.extend(items)
                            total_in += i_tok
                            total_out += o_tok
                        except Exception as e:
                            logger.error(f"Page processing failed for page {absolute_page_num}: {e}")

                        # Log progress every 25 pages
                        if completed_count % 25 == 0 or completed_count == len(pages_data):
                            logger.info(f"Job {job_id}: Progress {completed_count}/{len(pages_data)} pages completed")

                # Write output Excel
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
                logger.info(f"Job {job_id}: Completed. {len(all_items)} items found across {len(pages_data)} pages.")
                
            except Exception as e:
                logger.error(f"Job {job_id} failed: {e}")
                if doc:
                    doc.close()
                error_msg = str(e)[:500]
                query_db("UPDATE jobs SET status = 'failed', error_msg = %s WHERE id = %s", 
                        (error_msg, job_id), commit=True, conn=conn)

        query_db("UPDATE batches SET status = 'completed' WHERE id = %s", (batch_id,), commit=True, conn=conn)
        
    except Exception as e:
        logger.error(f"Batch {batch_id} failed CRITICALLY: {e}")
        try:
            query_db("UPDATE batches SET status = 'failed' WHERE id = %s", (batch_id,), commit=True, conn=conn)
        except:
            pass
    finally:
        if conn:
            conn.close()