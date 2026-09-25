import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import glob
import re
import fitz  # PyMuPDF
import io
import json
import openpyxl
from openpyxl import Workbook
from openpyxl.styles import PatternFill, Font, Alignment, Border, Side
from openpyxl.drawing.image import Image as OpenpyxlImage
from PIL import Image, ImageOps
from concurrent.futures import ThreadPoolExecutor, as_completed
import google.genai as genai
from dotenv import load_dotenv
import logging

from utils.prompt_assets import SYSTEM_PROMPT

load_dotenv()
logger = logging.getLogger(__name__)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

_gemini_client = None

def get_gemini_client():
    global _gemini_client
    if _gemini_client is None:
        if not GEMINI_API_KEY:
            raise ValueError("GEMINI_API_KEY environment variable is not set.")
        _gemini_client = genai.Client(api_key=GEMINI_API_KEY)
    return _gemini_client


# Load ALT Text Validation & Auto-fix Rules from utils/alt_text_rules.json
RULES_PATH = os.path.join(os.path.dirname(__file__), 'utils', 'alt_text_rules.json')
ALT_TEXT_RULES = {}
if os.path.exists(RULES_PATH):
    try:
        with open(RULES_PATH, 'r', encoding='utf-8') as f:
            ALT_TEXT_RULES = json.load(f).get("alt_text_validation_rules", {})
    except Exception as e:
        logger.warning(f"Could not load alt_text_rules.json: {e}")


def clean_alt_text_with_rules(text):
    """
    Applies rules from alt_text_rules.json to remove redundant image indicators,
    unnecessary action verbs, visual-only references, and decorative filler words.
    """
    if not text:
        return ""
    
    cleaned = text

    # 1. Redundant image indicators ("image of", "figure shows", "diagram of", etc.)
    redundant = ALT_TEXT_RULES.get("redundant_image_indicators", {}).get("words", [])
    for phrase in redundant:
        pattern = re.compile(re.escape(phrase), re.IGNORECASE)
        cleaned = pattern.sub("", cleaned)

    # 2. Visual-only references ("as you can see", "shown above", etc.)
    visual_refs = ALT_TEXT_RULES.get("visual_only_references", {}).get("words", [])
    for phrase in visual_refs:
        pattern = re.compile(re.escape(phrase), re.IGNORECASE)
        cleaned = pattern.sub("", cleaned)

    # 3. Decorative filler words ("nice", "beautiful", "high-quality", etc.)
    fillers = ALT_TEXT_RULES.get("decorative_filler_words", {}).get("words", [])
    for word in fillers:
        pattern = re.compile(r'\b' + re.escape(word) + r'\b', re.IGNORECASE)
        cleaned = pattern.sub("", cleaned)

    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    if cleaned and cleaned[0].islower():
        cleaned = cleaned[0].upper() + cleaned[1:]

    return cleaned


def render_cropped_page_image(page, dpi=200):
    """
    Renders PyMuPDF page, removes top header line and bottom footer,
    and trims whitespace around the artwork/math equation.
    """
    pix = page.get_pixmap(dpi=dpi)
    img = Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")
    w, h = img.size

    # Crop top 13% header area and bottom 6% footer area
    top_crop = int(h * 0.13)
    bottom_crop = int(h * 0.94)
    cropped = img.crop((0, top_crop, w, bottom_crop))

    # Auto-trim white margins around equation/figure
    inverted = ImageOps.invert(cropped)
    bbox = inverted.getbbox()
    if bbox:
        cw, ch = cropped.size
        padded_bbox = (
            max(0, bbox[0] - 20),
            max(0, bbox[1] - 20),
            min(cw, bbox[2] + 20),
            min(ch, bbox[3] + 20)
        )
        final_img = cropped.crop(padded_bbox)
    else:
        final_img = cropped

    buf = io.BytesIO()
    final_img.save(buf, format="PNG")
    buf.seek(0)
    return buf.getvalue(), final_img


def extract_header_filename(page_text):
    """
    Extracts 'File Name: <filename>' from page text (e.g. Ch01_Eqn001.eps, Figure 5.1.eps).
    """
    match = re.search(r'File Name:\s*([^\n\r]+)', page_text)
    if match:
        filename = match.group(1).strip()
        filename = re.sub(r'\s+Date.*', '', filename, flags=re.IGNORECASE).strip()
        return filename
    return ""


def generate_alt_text_parts(image_bytes, filename, model_name="gemini-2.5-pro"):
    """
    Calls Gemini API using system_prompt.json rules to produce WCAG 2.2 compliant Short & Long Alt Text.
    Returns dict with keys: 'short_alt', 'long_alt', 'context_type'
    """
    client = get_gemini_client()
    
    is_equation = ("eqn" in filename.lower() or "ineqn" in filename.lower())
    context_type = "Math" if is_equation else "General"
    
    prompt = (
        f"{SYSTEM_PROMPT}\n\n"
        f"SPECIFIC TASK FOR ART PROOF FILE '{filename}':\n"
        f"Analyze the attached visual element and return JSON with two fields:\n"
        f"1. 'short_alt': A concise summary under 125 characters.\n"
        f"2. 'long_alt': Complete detailed accessibility description.\n"
        f"   - For Math Equations: Provide ONLY plain, accessible spoken-English formula alt text for screen readers (spell out operations, symbols, fractions, powers, subscripts). Do NOT include LaTeX syntax, LaTeX code blocks, or 'LaTeX formula' headers.\n"
        f"   - For Figures: Provide diagram type, axes, labels, data trends, and key takeaway.\n\n"
        f"Return ONLY valid JSON format: {{\"short_alt\": \"...\", \"long_alt\": \"...\"}}"
    )

    image_part = genai.types.Part.from_bytes(data=image_bytes, mime_type="image/png")

    try:
        response = client.models.generate_content(
            model=model_name,
            contents=[image_part, prompt]
        )
        resp_text = response.text.strip() if response and response.text else ""
        
        # Parse JSON
        short_alt = ""
        long_alt = ""
        
        json_match = re.search(r'\{.*\}', resp_text, re.DOTALL)
        if json_match:
            try:
                data = json.loads(json_match.group(0))
                short_alt = data.get("short_alt", "")
                long_alt = data.get("long_alt", "")
            except Exception:
                pass
                
        if not long_alt:
            long_alt = resp_text

        if not short_alt:
            # Fallback short alt: first sentence or first 120 chars
            first_sent = long_alt.split('.')[0].strip()
            short_alt = first_sent[:120] if len(first_sent) > 120 else first_sent

        short_alt = clean_alt_text_with_rules(short_alt)
        long_alt = clean_alt_text_with_rules(long_alt)

        return {
            "short_alt": short_alt,
            "long_alt": long_alt,
            "context_type": context_type
        }

    except Exception as e:
        logger.error(f"Gemini API error for {filename}: {e}")
        return {
            "short_alt": f"Error for {filename}",
            "long_alt": f"Error generating alt text: {e}",
            "context_type": context_type
        }


def process_single_page(pdf_path, page_idx, model_name="gemini-2.5-pro"):
    doc = fitz.open(pdf_path)
    page = doc[page_idx]
    text = page.get_text()
    filename = extract_header_filename(text)

    pdf_filename = os.path.basename(pdf_path)

    if not filename:
        base_pdf = pdf_filename.replace('.pdf', '')
        filename = f"{base_pdf}_p{page_idx+1}.eps"

    img_bytes, pil_img = render_cropped_page_image(page)
    ai_res = generate_alt_text_parts(img_bytes, filename, model_name=model_name)
    
    short_alt = ai_res["short_alt"]
    long_alt = ai_res["long_alt"]
    word_count = len(long_alt.split()) if long_alt else 0

    if word_count < 25:
        category = "Simple"
    elif word_count < 150:
        category = "Moderate"
    else:
        category = "Complex"

    return {
        "pdf_filename": pdf_filename,
        "filename": filename,  # Figure number
        "page": page_idx + 1,
        "short_alt": short_alt,
        "long_alt": long_alt,
        "word_count": word_count,
        "category": category,
        "context_type": ai_res["context_type"],
        "domain": "Publishing",
        "img_bytes": img_bytes
    }


def process_proof_directory(folder_path, model_name="gemini-2.5-pro", max_workers=5):
    pdf_files = sorted(glob.glob(os.path.join(folder_path, "*_Art.pdf")))
    if not pdf_files:
        pdf_files = sorted(glob.glob(os.path.join(folder_path, "*.pdf")))

    if not pdf_files:
        raise FileNotFoundError(f"No PDF files found in directory: {folder_path}")

    # 1. Prepare target Excel workbook matching markup_Binder1_alt_text.xlsx layout
    wb = Workbook()
    ws = wb.active
    ws.title = "Alt Text"

    headers = [
        "File name",
        "Figure number",
        "Page number",
        "Image",
        "Short alt text",
        "Long alt text",
        "Word Count",
        "Category",
        "Context Type",
        "Domain"
    ]

    # Header styling
    header_fill = PatternFill(start_color="1F2937", end_color="1F2937", fill_type="solid")
    header_font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
    align_center = Alignment(horizontal="center", vertical="center", wrap_text=True)
    align_left = Alignment(horizontal="left", vertical="center", wrap_text=True)

    ws.append(headers)
    for col_num in range(1, len(headers) + 1):
        cell = ws.cell(row=1, column=col_num)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = align_center

    ws.row_dimensions[1].height = 28

    tasks = []
    for pdf_path in pdf_files:
        doc = fitz.open(pdf_path)
        for page_idx in range(len(doc)):
            tasks.append((pdf_path, page_idx))

    logger.info(f"Starting batch processing of {len(tasks)} proof pages across {len(pdf_files)} PDFs...")

    results = []
    completed = 0
    extracted_img_dir = os.path.join("outputs", "extracted_images")
    os.makedirs(extracted_img_dir, exist_ok=True)

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_task = {
            executor.submit(process_single_page, pdf_path, p_idx, model_name): (pdf_path, p_idx)
            for pdf_path, p_idx in tasks
        }

        for future in as_completed(future_to_task):
            pdf_path, p_idx = future_to_task[future]
            try:
                res = future.result()
                results.append(res)
                completed += 1
                logger.info(f"[{completed}/{len(tasks)}] Processed {res['filename']} (Page {res['page']})")
            except Exception as e:
                logger.error(f"Error processing page {p_idx+1} of {pdf_path}: {e}")

    # Sort results by PDF filename and page number
    results.sort(key=lambda x: (x['pdf_filename'], x['page']))

    # Check for original Mukherjee_AltText_Spreadsheet.xlsx to sync
    excel_path = os.path.join(folder_path, "Mukherjee_AltText_Spreadsheet.xlsx")
    mukherjee_wb = None
    mukherjee_ws = None
    mukherjee_map = {}

    if os.path.exists(excel_path):
        try:
            mukherjee_wb = openpyxl.load_workbook(excel_path)
            mukherjee_ws = mukherjee_wb.active
            for r in range(2, mukherjee_ws.max_row + 1):
                val = mukherjee_ws.cell(row=r, column=1).value
                if val:
                    norm_val = str(val).strip()
                    mukherjee_map[norm_val] = r
                    base_name = os.path.splitext(norm_val)[0]
                    mukherjee_map[base_name] = r
        except Exception as e:
            logger.warning(f"Could not load Mukherjee_AltText_Spreadsheet.xlsx: {e}")

    # Fill data rows into Workbook
    for item in results:
        row_vals = [
            item["pdf_filename"],
            item["filename"],  # Figure number (art proof filename)
            item["page"],
            "",  # Image thumbnail placeholder
            item["short_alt"],
            item["long_alt"],
            item["word_count"],
            item["category"],
            item["context_type"],
            item["domain"]
        ]
        ws.append(row_vals)
        current_row = ws.max_row

        # Sync to Mukherjee spreadsheet if row matches
        if mukherjee_ws:
            fname = item["filename"].strip()
            base_fname = os.path.splitext(fname)[0]
            m_row = mukherjee_map.get(fname) or mukherjee_map.get(base_fname)
            if m_row:
                mukherjee_ws.cell(row=m_row, column=3, value=item["long_alt"])

        # Embed Image Thumbnail into Column D
        crop_bytes = item.get("img_bytes")
        if crop_bytes:
            try:
                pil_img = Image.open(io.BytesIO(crop_bytes)).convert("RGB")
                clean_fig = re.sub(r'[^\w\.-]', '_', item["filename"])
                img_name = f"{item['pdf_filename']}_p{item['page']}_{clean_fig}.png"
                img_save_path = os.path.join(extracted_img_dir, img_name)
                pil_img.save(img_save_path)

                xl_img = OpenpyxlImage(img_save_path)
                max_size = 180
                ratio = min(max_size / xl_img.width, max_size / xl_img.height)
                if ratio < 1:
                    xl_img.width = int(xl_img.width * ratio)
                    xl_img.height = int(xl_img.height * ratio)

                cell_id = f"D{current_row}"
                ws.add_image(xl_img, cell_id)
                ws.row_dimensions[current_row].height = max(60, (xl_img.height * 0.75) + 10)
            except Exception as img_err:
                logger.warning(f"Could not embed image thumbnail for {item['filename']}: {img_err}")
                ws.row_dimensions[current_row].height = 40
        else:
            ws.row_dimensions[current_row].height = 40

    # Auto-adjust column widths
    column_widths = {
        'A': 18,  # File name
        'B': 22,  # Figure number
        'C': 12,  # Page number
        'D': 28,  # Image
        'E': 35,  # Short alt text
        'F': 65,  # Long alt text
        'G': 12,  # Word Count
        'H': 14,  # Category
        'I': 14,  # Context Type
        'J': 14   # Domain
    }

    for col_letter, width in column_widths.items():
        ws.column_dimensions[col_letter].width = width

    os.makedirs("outputs", exist_ok=True)
    
    # Base output filename on input spreadsheet name or folder name (e.g. Mukherjee_AltText_Spreadsheet_alttext.xlsx)
    if os.path.exists(excel_path):
        base_input_name = os.path.splitext(os.path.basename(excel_path))[0]
    else:
        base_input_name = os.path.basename(os.path.normpath(folder_path))
        
    out_excel_name = f"{base_input_name}_alttext.xlsx"
    out_excel_path = os.path.join("outputs", out_excel_name)
    wb.save(out_excel_path)

    if mukherjee_wb:
        try:
            mukherjee_wb.save(excel_path)
            # Also save copy in outputs with input filename _alttext.xlsx format
            mukherjee_wb.save(os.path.join("outputs", out_excel_name))
        except Exception as e:
            logger.warning(f"Could not save original Mukherjee spreadsheet: {e}")

    ui_results = []
    for r in results:
        ui_results.append({
            "filename": r["filename"],
            "page": r["page"],
            "pdf_name": r["pdf_filename"],
            "alt_text": r["long_alt"],
            "short_alt": r["short_alt"],
            "word_count": r["word_count"]
        })

    return ui_results, out_excel_path
