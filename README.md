# 🧠 AI Resume Builder

An AI-powered resume tool that reads your resume, runs an ATS check against a job description, rewrites your resume to maximise your score, and exports it as a Word document, PDF, or plain text — all from a simple browser interface.

Built with **FastAPI** (Python backend) + **Vanilla JavaScript** frontend + **OpenAI GPT-4o-mini**.

---

## 📸 Features

| Feature | Description |
|---|---|
| 📄 **File Upload** | Upload your resume as PDF, DOCX, or TXT |
| 📊 **ATS Score** | Scores your resume against a job description (0–100) with matched/missing keywords |
| 🔧 **Resume Optimiser** | Runs ATS check first, then rewrites your resume to maximise the score — without inventing facts |
| ✉️ **Cover Letter** | Writes a tailored, ATS-aware cover letter based on your resume and the job posting |
| 📝 **Word Export** | Downloads a styled `.docx` that mirrors a professional LaTeX resume template |
| 📑 **PDF Export** | Converts the Word doc to PDF using Microsoft Word (no LaTeX needed) |
| 📃 **TXT Export** | Plain text version — useful for pasting into online job application forms |

---

## 🗂️ Project Structure

```
ai-resume-app/
├── main.py            ← FastAPI backend (all AI logic lives here)
├── index.html         ← Frontend (open in browser — no build step needed)
├── requirements.txt   ← Python packages to install
├── .env               ← Your secret API key (you create this — never commit it)
├── .env.example       ← Safe template showing what .env should look like
└── .gitignore         ← Prevents .env from being pushed to GitHub
```

---

## ⚙️ Tech Stack

| Layer | Technology |
|---|---|
| Backend | Python 3.11+, FastAPI, Uvicorn |
| AI | OpenAI GPT-4o-mini |
| File Parsing | PyPDF2 (PDF), python-docx (DOCX) |
| Document Generation | python-docx (styled Word output) |
| PDF Export | docx2pdf (uses Microsoft Word on Windows) |
| Frontend | HTML + Vanilla JavaScript (no framework, no build step) |
| Config | python-dotenv (.env file) |

---

## 🚀 Setup & Installation

### Prerequisites

- Python 3.11 or higher — [download here](https://www.python.org/downloads/)
- Microsoft Word (for PDF export on Windows) — already installed on most Windows machines
- An OpenAI API key — [get one here](https://platform.openai.com/api-keys)

---

### Step 1 — Clone the repository

```bash
git clone https://github.com/your-username/ai-resume-app.git
cd ai-resume-app
```

---

### Step 2 — Create your `.env` file

Create a file called exactly `.env` in the project root (same folder as `main.py`):

```
OPENAI_API_KEY=sk-your-real-api-key-here
```

> ⚠️ Never share this file or commit it to GitHub. The `.gitignore` already excludes it.

---

### Step 3 — Install Python packages

```bash
pip install -r requirements.txt
```

This installs everything needed: FastAPI, OpenAI SDK, python-docx, PyPDF2, docx2pdf, and more.

---

### Step 4 — Start the backend server

```bash
uvicorn main:app --reload
```

You should see:
```
INFO:     Uvicorn running on http://127.0.0.1:8000
```

Leave this terminal window open while using the app.

---

### Step 5 — Open the frontend

Simply **double-click `index.html`** to open it in your browser.

No web server needed for the frontend — it talks directly to the FastAPI backend running on port 8000.

---

## 🧭 How to Use

### Optimise Resume
1. Upload your resume (PDF, DOCX, or TXT)
2. Paste the job description into the text box
3. Select **Optimise Resume** tab
4. Choose output format: **Word**, **PDF**, or **Plain Text**
5. Click **Generate & Download**

The app will:
- Run a silent ATS check to find keyword gaps
- Rewrite your resume to fill those gaps — using only facts from your original resume
- Download the polished file

---

### Cover Letter
1. Upload your resume
2. Paste the job description *(required)*
3. Select **Cover Letter** tab
4. Choose output format
5. Click **Generate & Download**

---

### ATS Score
1. Upload your resume
2. Paste the job description for best results *(optional — scores on general best practices if omitted)*
3. Select **ATS Score** tab
4. Click **Generate & Download**

The ATS panel will show:
- **Score** (0–100) with a colour-coded ring
- **Matched keywords** — already in your resume ✅
- **Missing keywords** — not in your resume ❌
- **Suggestions** — specific actions to improve

---

## 🤖 How the AI Pipeline Works

### Resume Mode (2 steps)

```
Step 1 — Silent ATS Pre-check
  → Compares resume to job description
  → Identifies: score, matched keywords, MISSING keywords

Step 2 — ATS-Optimised Rewrite
  → Rewrites summary to target the specific role
  → Weaves missing keywords naturally into experience bullets
  → Adds missing keywords to Skills if genuinely relevant
  → Reorders bullets (most relevant first)
  → STRICT RULE: never invents a job, degree, date, or fact
```

### Cover Letter Mode (2 steps)

```
Step 1 — Same ATS Pre-check as above

Step 2 — ATS-Aware Cover Letter
  → Opening hook connecting candidate to the role
  → Body paragraphs using matched keywords + bridging the gaps
  → Strong closing with call to action
  → Uses candidate's real name — no [placeholder] text
```

---

## 📤 Export Formats Explained

| Format | How it's generated | When to use |
|---|---|---|
| **Word (.docx)** | Built directly with python-docx, always works | When you want to edit before sending |
| **PDF** | Converts the Word doc using Microsoft Word via `docx2pdf` | When you need a final, print-ready file |
| **Plain Text (.txt)** | AI generates a clean formatted text version | For pasting into online job application forms |

> **PDF fallback:** If `docx2pdf` can't find Microsoft Word, the app downloads the `.docx` instead and shows a message. You can then open the `.docx` in Word and use **File → Save As → PDF** manually.

---

## 🔒 Environment Variables

| Variable | Required | Description |
|---|---|---|
| `OPENAI_API_KEY` | ✅ Yes | Your OpenAI API key from [platform.openai.com](https://platform.openai.com/api-keys) |

---

## 🐛 Troubleshooting

### `TypeError: Client.__init__() got an unexpected keyword argument 'proxies'`
Your OpenAI package is outdated. Fix:
```bash
pip install --upgrade openai
```

### PDF downloads as a text file / opens in Notepad
This means `docx2pdf` couldn't find Microsoft Word. Solutions:
- Make sure Microsoft Word is installed on your machine
- Or use the **Word (.docx)** format and export to PDF from Word manually

### `No module named 'dotenv'`
```bash
pip install python-dotenv
```

### The server starts but the browser shows a CORS error
Make sure you're accessing the backend at `http://localhost:8000` (not `https://`). Check that uvicorn is running and the terminal shows no errors.

### Empty file error on upload
The file must contain extractable text. Scanned PDFs (images of text) are not supported — use a text-based PDF or a DOCX file instead.

---

## 📦 Dependencies

```
fastapi==0.111.0
uvicorn[standard]==0.29.0
python-multipart==0.0.9
PyPDF2==3.0.1
python-docx==1.1.2
lxml>=4.9.0
docx2pdf>=0.1.8
openai>=1.52.0
python-dotenv==1.0.1
aiofiles==23.2.1
```

---

## 🛣️ Roadmap / Possible Future Features

- [ ] User authentication (login / saved resumes)
- [ ] Multiple resume templates to choose from
- [ ] LinkedIn profile import
- [ ] Job description auto-fetch from a URL
- [ ] Resume version history
- [ ] Cover letter tone selector (formal / casual / creative)

---

## 👨‍💻 Author

Built as a personal AI resume tool.  
Feel free to fork, improve, and make it your own.

---

## 📄 License

MIT License — free to use, modify, and distribute.
