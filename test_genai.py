import os
from dotenv import load_dotenv
from PIL import Image
import google.genai as genai

load_dotenv()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

def test_genai():
    client = genai.Client(api_key=GEMINI_API_KEY)
    image = Image.new('RGB', (100, 100))
    response = client.models.generate_content(
        model='gemini-2.5-flash',
        contents=["What is in this image?", image]
    )
    print("Response text:", response.text)
    if response.usage_metadata:
        print("In:", response.usage_metadata.prompt_token_count)
        print("Out:", response.usage_metadata.candidates_token_count)

if __name__ == '__main__':
    test_genai()
