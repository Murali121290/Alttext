"""
This module contains the prompt engineering assets for the AI Alt Text Generator.
It loads the prompt structure from a JSON file for easier management.
"""
import json
import os

PROMPT_JSON_PATH = os.path.join(os.path.dirname(__file__), 'system_prompt.json')

# Supported English spelling variants for generated alt text.
DEFAULT_SPELLING = "US"
SPELLING_VARIANTS = {
    "US": (
        "American English",
        "Use American English spelling for EVERY word (e.g., color, center, organize, analyze, "
        "labeled, gray, behavior, meter, fiber, hemoglobin, esophagus, pediatric, program). "
        "Do NOT use British spellings."
    ),
    "UK": (
        "British English",
        "Use British English spelling for EVERY word (e.g., colour, centre, organise, analyse, "
        "labelled, grey, behaviour, metre, fibre, haemoglobin, oesophagus, paediatric, programme). "
        "Do NOT use American spellings."
    ),
}


def normalize_spelling(value):
    """Return 'UK' or 'US' from user input; anything unrecognised falls back to US."""
    v = str(value or "").strip().upper()
    if v in ("UK", "GB", "BRITISH", "EN-GB"):
        return "UK"
    return DEFAULT_SPELLING


def spelling_name(value):
    return SPELLING_VARIANTS[normalize_spelling(value)][0]


def spelling_instruction(value):
    """Prompt block that tells the model which spelling variant to use."""
    return f"SPELLING VARIANT (MANDATORY):\n{SPELLING_VARIANTS[normalize_spelling(value)][1]}"


def load_system_prompt():
    try:
        with open(PROMPT_JSON_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)

        p = data.get("system_prompt", {})

        # Construct the string
        prompt_text = f"{p.get('role', '')}\n\n{p.get('goal', '')}\n\n"

        for section in p.get("sections", []):
            prompt_text += f"{section.get('title', '')}\n"
            for line in section.get("instructions", []):
                prompt_text += f"{line}\n"
            prompt_text += "\n"

        out_fmt = p.get("output_format", {})

        prompt_text += f"\nOUTPUT FORMAT:\n{out_fmt.get('instruction', '')}\n"

        if "schema" in out_fmt:
            prompt_text += f"Schema fields required:\n{json.dumps(out_fmt['schema'], indent=2)}\n"
        elif "example" in out_fmt:
            prompt_text += f"With Example:\n{out_fmt.get('example', '')}\n"
        elif "fields" in out_fmt:
            prompt_text += f"Fields required:\n{json.dumps(out_fmt['fields'], indent=2)}\n"

        prompt_text += f"\n{out_fmt.get('empty_case', '')}"
        return prompt_text
    except Exception as e:
        print(f"Error loading prompt JSON: {e}")
        return "Error loading system prompt."


BASE_SYSTEM_PROMPT = load_system_prompt()


def get_system_prompt(spelling=DEFAULT_SPELLING):
    """Full system prompt with the chosen spelling variant (US or UK) appended."""
    return f"{BASE_SYSTEM_PROMPT}\n\n{spelling_instruction(spelling)}"


# Backwards-compatible default (US spelling) for callers that don't pass a variant.
SYSTEM_PROMPT = get_system_prompt(DEFAULT_SPELLING)
