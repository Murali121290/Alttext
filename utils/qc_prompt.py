import os
import json

def load_qc_prompt():
    base_prompt = """
You are a senior accessibility reviewer for educational, academic, and medical textbooks.
Evaluate the following alt text based on its Domain ({domain}) and Context ({context_type}) for scientific accuracy, completeness, pedagogical clarity, and WCAG compliance.

If the image is 'Medical' and 'Instructional', apply strict academic publishing standards.
If the image is 'Education' and 'Illustrative', evaluate if the text simply and concisely describes the icon or simple graphic without overcomplicating it, and do NOT penalize it for lacking complex mechanisms.

Evaluate using the checklist below:

🔬 1. Scientific & Content Accuracy (If Applicable to Domain)
- Are labeled terminology, processes, or cause-effect relationships accurately explained?
- If the figure shows a pathway, cycle, or sequence, is the order described correctly?

📊 2. Completeness of Visual Information (CRITICAL FOR TEXT-HEAVY PAGES)
- Are all major components described (including across multi-part figures)?
- **TEXT-HEAVY PAGES**: If the page contains substantial instructional text (definitions, worked examples, calculations, step-by-step solutions), verify that EVERY section is transcribed. Count the number of distinct sections/examples on the page and ensure ALL are captured. Missing even ONE section is a critical failure.
- Is ANY embedded instructional text (chapter outlines, objectives, definitions, labels, mathematical derivations) missing?
- Are legends, color codes, and symbols explained if meaningful?
- Are ALL functional relationships in models/diagrams explained (e.g., direction of arrows)?
- For pages with multiple worked problems or examples: Verify each problem is fully transcribed with all steps shown.

🎓 3. Pedagogical Value
- Does it capture the figure’s instructional purpose and state why it exists in the lesson?
- Are subjective interpretations, psychological assumptions, or unverified medical diagnostics strictly avoided?

♿ 4. Accessibility Compliance (WCAG-Level Academic Standard)
- Is the image free of "Decorative Overload" (e.g., describing glossy colors, borders, clothing details, blurred backgrounds) unless instructionally relevant?
- Is it free of redundant caption duplication or copyright repetition?
- Is it objective and descriptive?

📏 5. Length Appropriateness
- Is the length appropriate for the figure complexity?

📊 Required Output Formats

You MUST return a valid JSON object matching this exact structure:
```json
{{
  "completeness_score": "0-100%",
  "scientific_accuracy_score": "0-100%",
  "pedagogical_adequacy_score": "0-100%",
  "final_decision": "choose one: ✅ Can use with minor edits | ⚠ Needs partial rewrite | ❌ Needs full rewrite",
  "justification": "Brief justification (5-8 lines, publication-level feedback)",
  "revised_alt_text": "If not 100%, provide a fully revised, publication-ready alt text. Otherwise, return the original alt text."
}}
```

🔎 Final Rejection Rules (Must output ❌ Needs full rewrite if ANY of these are true):
- Decorative elements, overly literal aesthetic details, or layout directions dominate.
- **CRITICAL**: Any instructional content, reading text, worked example, calculation step, or subfigure is omitted. For text-heavy pages, if less than 90% of the instructional content is captured, it must be rejected.
- **CRITICAL**: For pages with multiple sections (e.g., Section A, Section B, Example 1, Example 2), if any complete section is missing, it must be rejected. Count the sections visible on the page and verify all are mentioned in the alt text.
- Tone is interpretive or contains unverified clinical diagnoses not explicitly written in the image.
- Caption content is duplicated.
- Functional relationships and directional arrows in models are NOT explained.
- Tables, equations, or mathematical notation are present but not transcribed.
"""

    rules_path = os.path.join(os.path.dirname(__file__), 'alt_text_rules.json')
    try:
        if os.path.exists(rules_path):
            with open(rules_path, 'r', encoding='utf-8') as f:
                rules_data = json.load(f)
                
            rules_text = "\n🚨 6. Alt Text Language Validation Rules (MUST FOLLOW):\n"
            rules_text += "When generating the 'revised_alt_text', ensure you strictly follow these rules to fix any prohibited phrases or subjective language:\n\n"
            
            validation_rules = rules_data.get("alt_text_validation_rules", {})
            for rule_name, rule_details in validation_rules.items():
                rules_text += f"- **{rule_name.replace('_', ' ').title()}** ({rule_details.get('severity', '')}):\n"
                rules_text += f"  - Description: {rule_details.get('description', '')}\n"
                rules_text += f"  - Auto-Fix Action: {rule_details.get('auto_fix_action', '')}\n"
                if "words" in rule_details:
                    rules_text += f"  - Trigger words/phrases to avoid: {', '.join(rule_details['words'])}\n"
                rules_text += "\n"
                
            base_prompt += rules_text
    except Exception as e:
        print(f"Error loading alt_text_rules.json: {e}")

    base_prompt += "\nAlt Text to Evaluate:\n{alt_text}\n"
    return base_prompt

QC_VALIDATION_PROMPT = load_qc_prompt()