# PDF Alt Text Generator

This is a Flask application that extracts images from PDF files, generates alt text (short and long) using Google's Gemini API, and exports the results to an Excel file.

## Features
- Upload PDF files.
- extract images page-wise.
- Generate Alt Text using Gemini 1.5 Flash.
- Export results to Excel with columns: File name, Figure number, Page number, Short alt text, Long alt text.

   pip install -r requirements.txt
   
   
   *Option A: Environment Variable*
   Edit the `.env` file and add your key:
   ```
   GEMINI_API_KEY=your_actual_api_key_here
   ```
   
   *Option B: Web Interface*
   You can also paste your API key directly in the web interface when uploading a file.

## Running the App

Run the Flask application:
```bash
python app.py
```

Access the application in your browser at:
`http://127.0.0.1:5000`

## Usage
1. Open the web page.
2. Enter your API Key (if not set in .env).
3. Select a PDF file.
4. Click "Upload & Process".
5. Wait for processing (it may take a few seconds per image).
6. Download the generated Excel file.
