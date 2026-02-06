"""
This module contains the prompt engineering assets for the AI Alt Text Generator.
It loads the prompt structure from a JSON file for easier management.
"""
import json
import os

PROMPT_JSON_PATH = os.path.join(os.path.dirname(__file__), 'system_prompt.json')

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
        prompt_text += f"{out_fmt.get('instruction', '')}\nWith Example:\n{out_fmt.get('example', '')}\n{out_fmt.get('empty_case', '')}"
        
        return prompt_text
    except Exception as e:
        print(f"Error loading prompt JSON: {e}")
        return "Error loading system prompt."

SYSTEM_PROMPT = load_system_prompt()
