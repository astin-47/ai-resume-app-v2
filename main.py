# =============================================================
# main.py  —  AI Resume Builder  (FastAPI Backend)
# =============================================================
#
#  What this file does (plain English):
#  ─────────────────────────────────────
#  1. Receives a resume file (PDF / DOCX / TXT) from the browser
#  2. Extracts the text out of that file
#  3. Sends that text to OpenAI (GPT-4o-mini) with instructions
#  4. Gets back polished content and builds a LaTeX document
#  5. Compiles the LaTeX into a real PDF  (if pdflatex is installed)
#     OR returns the .tex source as a fallback
#  6. Also handles:
#       • Cover letter generation  (TXT or PDF)
#       • ATS scoring              (returns a JSON score card)
#
#  Endpoints
#  ─────────
#   POST /upload    – extract & preview text from an uploaded file
#   POST /generate  – the main AI generation endpoint
#   GET  /          – health-check (just confirms the server is up)
#
# =============================================================

import os, io, json, tempfile, subprocess
from fastapi import FastAPI, File, UploadFile, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse

# python-dotenv reads the .env file so we can keep secrets out of code
from dotenv import load_dotenv
load_dotenv()   # looks for .env in the same folder as this file

import PyPDF2           # reads text from PDF files
from docx import Document   # reads text from DOCX files
from openai import OpenAI

# ── create the OpenAI client (key comes from .env) ───────────
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# ── create the FastAPI app ────────────────────────────────────
app = FastAPI(title="AI Resume Builder", version="1.0.0")

# ── CORS: allow the HTML file (opened from file://) to call us ─
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],        # lock this down in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

ALLOWED_EXT = {".pdf", ".docx", ".txt"}


# =============================================================
#  HELPER – extract plain text from the uploaded file bytes
# =============================================================
def extract_text(data: bytes, filename: str) -> str:
    """
    Takes the raw bytes of an uploaded file and returns plain text.
    Raises ValueError with a human-readable message on any problem.
    """
    ext = os.path.splitext(filename)[1].lower()

    if ext not in ALLOWED_EXT:
        raise ValueError(
            f"'{ext}' is not supported. Please upload a PDF, DOCX, or TXT file."
        )

    text = ""

    if ext == ".pdf":
        try:
            reader = PyPDF2.PdfReader(io.BytesIO(data))
            for page in reader.pages:
                chunk = page.extract_text()
                if chunk:
                    text += chunk + "\n"
        except Exception as e:
            raise ValueError(f"Could not read PDF: {e}")

    elif ext == ".docx":
        try:
            doc = Document(io.BytesIO(data))
            for para in doc.paragraphs:
                text += para.text + "\n"
        except Exception as e:
            raise ValueError(f"Could not read DOCX: {e}")

    elif ext == ".txt":
        try:
            text = data.decode("utf-8", errors="replace")
        except Exception as e:
            raise ValueError(f"Could not read TXT: {e}")

    text = text.strip()
    if not text:
        raise ValueError(
            "The file appears to be empty or has no extractable text."
        )
    return text


# =============================================================
#  HELPER – send a prompt to OpenAI and get back a string
# =============================================================
def ask_ai(system_prompt: str, user_prompt: str) -> str:
    """
    Sends one request to GPT-4o-mini and returns the text reply.
    Straightforward wrapper so the rest of the code stays clean.
    """
    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_prompt},
        ],
        temperature=0.7,
        max_tokens=3000,
    )
    return resp.choices[0].message.content.strip()


# =============================================================
#  HELPER – build a LaTeX document from AI-generated sections
# =============================================================
def build_latex(resume_text: str, job_description: str = "") -> str:
    """
    Asks the AI to fill in each resume section, then assembles
    the complete LaTeX document using the exact template format
    the user provided.

    The AI is asked for JSON so we can reliably slot each section
    into the right place in the template.
    """

    system = """You are an expert resume writer and LaTeX specialist.

The user will give you a raw resume (and optionally a job description).
Your job: extract and rewrite all information into these 6 sections,
then return ONLY a valid JSON object (no markdown fences, no commentary):

{
  "name":        "Full Name",
  "phone":       "+XX XXXXXXXXXX",
  "email":       "email@example.com",
  "linkedin":    "linkedin.com/in/username",
  "summary":     "3-4 sentence professional summary. ATS optimised.",
  "skills": [
    {"category": "Category Name", "items": "item1, item2, item3"}
  ],
  "experience": [
    {
      "title":    "Job Title",
      "company":  "Company Name, Location",
      "dates":    "Month YYYY -- Month YYYY",
      "bullets":  ["Bullet 1 text", "Bullet 2 text"]
    }
  ],
  "education": [
    {
      "institution": "School Name",
      "degree":      "Degree Name",
      "dates":       "YYYY -- YYYY",
      "grade":       "GPA / CGPA / Grade (or empty string)"
    }
  ],
  "certifications": [
    {"name": "Certification Name", "year": "YYYY"}
  ]
}

Rules:
- Use strong action verbs in bullet points
- Quantify achievements where possible
- Do NOT invent any information not in the original resume
- Keep all dates, companies, and facts exactly as given
- Return ONLY the JSON object, nothing else
"""

    user = f"Resume:\n{resume_text}"
    if job_description.strip():
        user += f"\n\nTarget Job Description:\n{job_description}"

    raw = ask_ai(system, user)

    # strip accidental markdown fences if the AI adds them
    clean = raw.strip().lstrip("```json").lstrip("```").rstrip("```").strip()

    try:
        d = json.loads(clean)
    except json.JSONDecodeError:
        raise ValueError(
            "AI returned unexpected data. Please try again."
        )

    # ── Escape LaTeX special characters ──────────────────────
    def tex(s):
        """Escape &  %  $  #  _  {  }  ~  ^  \\  for LaTeX"""
        s = str(s)
        replacements = [
            ("\\", "\\textbackslash{}"),
            ("&",  "\\&"),
            ("%",  "\\%"),
            ("$",  "\\$"),
            ("#",  "\\#"),
            ("_",  "\\_"),
            ("{",  "\\{"),
            ("}",  "\\}"),
            ("~",  "\\textasciitilde{}"),
            ("^",  "\\textasciicircum{}"),
        ]
        for old, new in replacements:
            s = s.replace(old, new)
        return s

    # ── Build skills block ────────────────────────────────────
    skills_lines = ""
    for sk in d.get("skills", []):
        skills_lines += (
            f"    \\titleItem{{{tex(sk['category'])}}}"
            f"{{: {tex(sk['items'])} }} \\\\\n"
        )

    # ── Build experience block ────────────────────────────────
    exp_block = ""
    for job in d.get("experience", []):
        bullets = "\n".join(
            f"        \\resumeItem{{{tex(b)}}}" for b in job.get("bullets", [])
        )
        exp_block += f"""
      \\resumeProjectHeading
           {{\\titleItem{{{tex(job['title'])}}} $|$ \\emph{{{tex(job['company'])}}}}}{{{tex(job['dates'])}}}
      \\resumeItemListStart
{bullets}
      \\resumeItemListEnd
"""

    # ── Build education block ─────────────────────────────────
    edu_block = ""
    for edu in d.get("education", []):
        grade_part = f"\\textbf{{{tex(edu['grade'])}}}  " if edu.get("grade") else ""
        edu_block += (
            f"    \\resumeItem{{\\textbf{{{tex(edu['institution'])}}} "
            f"{grade_part}"
            f"\\textit{{{tex(edu['degree'])}}}  "
            f"\\textit{{{tex(edu['dates'])}}} }}\\\\\n"
        )

    # ── Build certifications block ────────────────────────────
    cert_block = ""
    for c in d.get("certifications", []):
        year_part = f"\\emph{{$|$ {tex(c['year'])}}}" if c.get("year") else ""
        cert_block += (
            f"          \\resumeItem{{\\textbf{{{tex(c['name'])}}} {year_part}}}\n"
        )

    # ── Assemble the full LaTeX document ─────────────────────
    # This is the exact template the user supplied, with the
    # dynamic sections filled in.
    latex = r"""\documentclass[letterpaper,12pt]{article}
\usepackage{latexsym}
\usepackage[empty]{fullpage}
\usepackage{titlesec}
\usepackage{marvosym}
\usepackage[usenames,dvipsnames]{color}
\usepackage{verbatim}
\usepackage{enumitem}
\usepackage[hidelinks]{hyperref}
\usepackage{fancyhdr}
\usepackage[english]{babel}
\usepackage{tabularx}
\usepackage{amsmath}
\usepackage{soul}
\usepackage{setspace}
\input{glyphtounicode}
\usepackage[margin=0.5in]{geometry}
\usepackage[default]{sourcesanspro}

\pagestyle{fancy}
\fancyhf{}
\fancyfoot{}
\renewcommand{\headrulewidth}{0pt}
\renewcommand{\footrulewidth}{0pt}
\urlstyle{same}
\raggedbottom
\raggedright
\setlength{\tabcolsep}{0in}

\titleformat{\section}{
  \vspace{-10pt}\scshape\raggedright\Large\fontsize{14}{14}\selectfont
}{}{0em}{}[\color{black}\titlerule \vspace{-4pt}]

\pdfgentounicode=1

\def\spaceforrole{ }
\definecolor{lightyellow}{cmyk}{0.00, 0.05, 0.20, 0.00}
\sethlcolor{lightyellow}

\newcommand{\sectionspace}{\vspace{-14pt}}
\newcommand{\subheadingtitlevspace}{\vspace{-3pt}}

\newcommand{\resumeItem}[1]{\item{{#1 \vspace{-4pt}}}}
\newcommand{\titleItem}[1]{\textbf{#1}}
\newcommand{\highlight}[1]{\textsl{\textbf{#1}}}

\newcommand{\resumeSubheading}[4]{
  \item
     \begin{tabular*}{0.97\textwidth}[t]{l@{\extracolsep{\fill}}l@{}l}
      {#1} & \titleItem{#3} | {#2} & \textit{#4}\\
    \end{tabular*}\vspace{-4pt}
}

\newcommand{\resumeSubSubheading}[2]{
    \item
    \begin{tabular*}{0.97\textwidth}{l@{\extracolsep{\fill}}r}
      \textit{#1} & \textit{#2} \\
    \end{tabular*}\vspace{-4pt}
}

\newcommand{\resumeProjectHeading}[2]{
    \item
    \begin{tabular*}{0.97\textwidth}{l@{\extracolsep{\fill}}r}
      #1 & \textit{ #2} \\
    \end{tabular*}\vspace{-4pt}
}

\newcommand{\resumeSubHeadingListStart}{\subheadingtitlevspace\begin{itemize}[leftmargin=0.15in, label={}]}
\newcommand{\resumeSubHeadingListEnd}{\end{itemize}}
\newcommand{\resumeItemListStart}{\begin{itemize}}
\newcommand{\resumeItemListEnd}{\end{itemize}\vspace{-6pt}}

\setstretch{1.1}

\begin{document}

%---------- HEADING ----------
\begin{flushleft}
    {\Huge \textbf{""" + tex(d.get("name","")) + r"""}} \\
    \textit{} $|$
    \textit{""" + tex(d.get("phone","")) + r"""} $|$
    \href{mailto:""" + d.get("email","") + r"""}{{\textit{""" + tex(d.get("email","")) + r"""}}} $|$
    \href{https://""" + d.get("linkedin","") + r"""}{{\textit{""" + tex(d.get("linkedin","")) + r"""}}}
    \vspace{-7pt}
\end{flushleft}

%---------- PROFESSIONAL SUMMARY ----------
\section{Professional Summary}
\vspace{-4pt}
\begin{itemize}[leftmargin=0.15in, label={}]
    {\item{
     {""" + tex(d.get("summary","")) + r"""} \\
    }}
\end{itemize}
\sectionspace

%---------- TECHNICAL SKILLS ----------
\section{Technical Skills}
\subheadingtitlevspace
\begin{itemize}[leftmargin=0.15in, label={}]
    {\item{
""" + skills_lines + r"""    }}
\end{itemize}
\sectionspace

%---------- EXPERIENCE ----------
\section{Experience}
  \resumeSubHeadingListStart
""" + exp_block + r"""  \resumeSubHeadingListEnd
\sectionspace

%---------- CERTIFICATIONS ----------
\section{Awards and Certifications}
    \resumeSubHeadingListStart
""" + cert_block + r"""    \resumeSubHeadingListEnd
\sectionspace

%---------- EDUCATION ----------
\section{Education}
  \resumeSubHeadingListStart
""" + edu_block + r"""  \resumeSubHeadingListEnd
\sectionspace

\end{document}
"""
    return latex


# =============================================================
#  HELPER – compile LaTeX to PDF
#  Returns PDF bytes on success, None if pdflatex not available
# =============================================================
def latex_to_pdf(latex_src: str):
    try:
        with tempfile.TemporaryDirectory() as tmp:
            tex = os.path.join(tmp, "resume.tex")
            pdf = os.path.join(tmp, "resume.pdf")
            with open(tex, "w", encoding="utf-8") as f:
                f.write(latex_src)
            # run twice so cross-references resolve correctly
            for _ in range(2):
                result = subprocess.run(
                    ["pdflatex", "-interaction=nonstopmode",
                     "-output-directory", tmp, tex],
                    capture_output=True, timeout=60,
                )
            if result.returncode == 0 and os.path.exists(pdf):
                with open(pdf, "rb") as f:
                    return f.read()
    except Exception:
        pass
    return None


# =============================================================
#  HELPER – save bytes to a temp file and return a FileResponse
# =============================================================
def temp_file_response(data: bytes, suffix: str, media: str, filename: str,
                       extra_headers: dict = None):
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    tmp.write(data)
    tmp.close()
    return FileResponse(
        path=tmp.name,
        media_type=media,
        filename=filename,
        headers=extra_headers or {},
    )


# =============================================================
#  ENDPOINT  GET /
#  Health check — open http://localhost:8000 to confirm it works
# =============================================================
@app.get("/")
def root():
    return {"status": "ok", "message": "AI Resume Builder is running ✅"}


# =============================================================
#  ENDPOINT  POST /upload
#  Accepts a file, extracts and returns the text for preview
# =============================================================
@app.post("/upload")
async def upload(file: UploadFile = File(...)):
    """
    Step 1 (optional): upload your resume to preview the extracted text
    before generating anything.

    Request  : multipart/form-data  →  field name = "file"
    Response : { filename, characters, text }
    """
    if not file or not file.filename:
        raise HTTPException(400, "No file received.")
    data = await file.read()
    if not data:
        raise HTTPException(400, "The uploaded file is empty.")
    try:
        text = extract_text(data, file.filename)
    except ValueError as e:
        raise HTTPException(422, str(e))
    return {"filename": file.filename, "characters": len(text), "text": text}


# =============================================================
#  ENDPOINT  POST /generate
#  The main AI endpoint — handles all three modes
# =============================================================
@app.post("/generate")
async def generate(
    file:            UploadFile = File(...),
    job_description: str        = Form(default=""),
    option:          str        = Form(default="resume"),
    # option values:
    #   "resume"  →  optimise & generate a full resume
    #   "cover"   →  write a cover letter
    #   "ats"     →  score the resume against the job description
    output_format:   str        = Form(default="pdf"),
    # output_format values:
    #   "pdf"  →  compiled PDF  (falls back to .tex if pdflatex missing)
    #   "txt"  →  plain-text file
):
    # ── validate inputs ───────────────────────────────────────
    if option not in {"resume", "cover", "ats"}:
        raise HTTPException(400, f"Unknown option '{option}'. Use: resume | cover | ats")
    if output_format not in {"pdf", "txt"}:
        raise HTTPException(400, f"Unknown format '{output_format}'. Use: pdf | txt")
    if not file or not file.filename:
        raise HTTPException(400, "Please attach your resume file.")

    # ── read & extract resume text ────────────────────────────
    data = await file.read()
    if not data:
        raise HTTPException(400, "Uploaded file is empty.")
    try:
        resume_text = extract_text(data, file.filename)
    except ValueError as e:
        raise HTTPException(422, str(e))

    # ─────────────────────────────────────────────────────────
    #  MODE 1  —  ATS SCORING
    #  Always returns JSON regardless of output_format
    # ─────────────────────────────────────────────────────────
    if option == "ats":
        system = """You are an Applicant Tracking System (ATS) expert.
Analyse the resume against the job description and return ONLY valid JSON
(no markdown, no explanation) with exactly these keys:

{
  "score":            <integer 0-100>,
  "level":            "<Excellent | Good | Fair | Poor>",
  "matched_keywords": ["kw1", "kw2"],
  "missing_keywords": ["kw3", "kw4"],
  "suggestions":      ["specific actionable tip 1", "tip 2"]
}

Scoring guide:
  90-100  Excellent — very strong match
  70-89   Good      — solid, minor gaps
  50-69   Fair      — noticeable gaps
  0-49    Poor      — significant mismatch
"""
        user = (
            f"Resume:\n{resume_text}\n\n"
            f"Job Description:\n{job_description or 'No job description provided — score on general best practices.'}"
        )
        try:
            raw = ask_ai(system, user)
            clean = raw.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
            result = json.loads(clean)
        except json.JSONDecodeError:
            result = {
                "score": 0, "level": "Unknown",
                "matched_keywords": [], "missing_keywords": [],
                "suggestions": [raw],
                "_parse_error": True,
            }
        except Exception as e:
            raise HTTPException(500, f"OpenAI error: {e}")
        return JSONResponse(content=result)

    # ─────────────────────────────────────────────────────────
    #  MODE 2  —  COVER LETTER
    # ─────────────────────────────────────────────────────────
    if option == "cover":
        if not job_description.strip():
            raise HTTPException(
                400,
                "A job description is required to generate a cover letter. "
                "Please paste the job posting into the Job Description box."
            )
        system = (
            "You are a professional cover letter writer. "
            "Write a compelling, personalised cover letter (max 380 words). "
            "Tone: confident, genuine, specific. "
            "Include: opening hook, 2 paragraphs matching experience to the role, "
            "and a strong closing. Do NOT write '[Your Name]' placeholders — "
            "use the candidate's actual name from the resume."
        )
        user = f"Resume:\n{resume_text}\n\nJob Description:\n{job_description}"
        try:
            cover_text = ask_ai(system, user)
        except Exception as e:
            raise HTTPException(500, f"OpenAI error: {e}")

        if output_format == "txt":
            return temp_file_response(
                cover_text.encode("utf-8"), ".txt",
                "text/plain", "cover_letter.txt"
            )
        else:
            # PDF for cover letter: simple LaTeX letter
            system2 = (
                "Convert the cover letter text below into valid LaTeX source code "
                "using the 'article' documentclass, 1-inch margins, 12pt font. "
                "Return ONLY the LaTeX code, no markdown fences."
            )
            try:
                latex_src = ask_ai(system2, cover_text)
            except Exception as e:
                raise HTTPException(500, f"OpenAI error during LaTeX conversion: {e}")

            pdf_bytes = latex_to_pdf(latex_src)
            if pdf_bytes:
                return temp_file_response(
                    pdf_bytes, ".pdf", "application/pdf", "cover_letter.pdf"
                )
            else:
                # pdflatex not available → return .tex with a note
                return temp_file_response(
                    latex_src.encode("utf-8"), ".tex",
                    "application/x-tex", "cover_letter.tex",
                    {"X-Fallback": "pdflatex not installed; returning LaTeX source"}
                )

    # ─────────────────────────────────────────────────────────
    #  MODE 3  —  RESUME  (the main mode)
    # ─────────────────────────────────────────────────────────
    if option == "resume":
        try:
            latex_src = build_latex(resume_text, job_description)
        except ValueError as e:
            raise HTTPException(422, str(e))
        except Exception as e:
            raise HTTPException(500, f"Error building resume: {e}")

        if output_format == "txt":
            # For TXT: ask AI to produce a clean plain-text version
            system_txt = (
                "Rewrite the resume as clean plain text, "
                "with section headers in ALL CAPS separated by dashes. "
                "No LaTeX, no markdown. Keep all facts exactly as given."
            )
            try:
                txt_content = ask_ai(system_txt, resume_text)
            except Exception as e:
                raise HTTPException(500, f"OpenAI error: {e}")
            return temp_file_response(
                txt_content.encode("utf-8"), ".txt",
                "text/plain", "resume.txt"
            )

        else:  # pdf (or fallback to tex)
            pdf_bytes = latex_to_pdf(latex_src)
            if pdf_bytes:
                return temp_file_response(
                    pdf_bytes, ".pdf", "application/pdf", "resume.pdf"
                )
            else:
                # Return LaTeX source — user can paste it into overleaf.com
                return temp_file_response(
                    latex_src.encode("utf-8"), ".tex",
                    "application/x-tex", "resume.tex",
                    {"X-Fallback": "pdflatex not installed; open resume.tex at overleaf.com"}
                )
