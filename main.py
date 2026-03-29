# =============================================================
# main.py  —  AI Resume Builder  v4.0  (with Supabase Auth)
# =============================================================
#
#  NEW IN v4: User Authentication
#  ────────────────────────────────
#  Every request to /generate and /upload now requires a valid
#  Supabase JWT token in the Authorization header.
#
#  How it works:
#    1. User logs in on the frontend using Supabase JS SDK
#    2. Supabase gives the browser a JWT access token
#    3. Frontend sends that token with every API request:
#         Authorization: Bearer <token>
#    4. Backend calls Supabase to verify the token is real
#    5. If valid  → extract user ID, allow the request
#    6. If invalid → return 401 Unauthorized
#
#  Usage tracking:
#    Each successful /generate call is logged to the
#    "usage_logs" table in Supabase with the user's ID,
#    the option used (resume/cover/ats), and a timestamp.
#    This lets you see who uses the app and how often.
#
#  ENDPOINTS:
#  ──────────
#   GET  /          — health check (no auth required)
#   POST /upload    — extract text  (auth required)
#   POST /generate  — AI generation (auth required)
#
# =============================================================

import os, io, json, tempfile, shutil, subprocess
from fastapi import FastAPI, File, UploadFile, Form, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse

from dotenv import load_dotenv
load_dotenv()

import PyPDF2
import httpx                         # to call Supabase REST API
from docx import Document
from docx.shared import Pt, Inches
from docx.oxml.ns import qn
from docx.oxml import OxmlElement
from openai import OpenAI

# ── Clients ───────────────────────────────────────────────────
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# Supabase project credentials — set these in Render environment variables
SUPABASE_URL     = os.getenv("SUPABASE_URL")      # e.g. https://xxxx.supabase.co
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY") # public anon key from Supabase dashboard

# ── App ───────────────────────────────────────────────────────
app = FastAPI(title="AI Resume Builder", version="4.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

ALLOWED_EXT = {".pdf", ".docx", ".txt"}
DOCX_MIME   = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


# =============================================================
#  AUTH HELPER — verify the Supabase JWT token
#
#  The frontend sends:  Authorization: Bearer <token>
#  This function:
#    1. Reads that token from the request header
#    2. Calls Supabase to check it is valid
#    3. Returns the user's ID (a unique string like "uuid-xxx")
#    4. Raises HTTP 401 if anything is wrong
#
#  Why call Supabase instead of verifying locally?
#  Simpler for beginners — no JWT library needed.
#  Supabase's /auth/v1/user endpoint does the verification for us.
# =============================================================
async def get_current_user(request: Request) -> str:
    # Read the Authorization header
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail="Not logged in. Please sign in to use this feature."
        )

    token = auth_header.split(" ", 1)[1]  # extract the token after "Bearer "

    # Ask Supabase: is this token valid?
    try:
        async with httpx.AsyncClient() as http:
            response = await http.get(
                f"{SUPABASE_URL}/auth/v1/user",
                headers={
                    "Authorization": f"Bearer {token}",
                    "apikey": SUPABASE_ANON_KEY,
                },
                timeout=10,
            )
    except Exception:
        raise HTTPException(status_code=503, detail="Auth service unreachable. Try again.")

    if response.status_code != 200:
        raise HTTPException(
            status_code=401,
            detail="Session expired or invalid. Please log in again."
        )

    user_data = response.json()
    return user_data.get("id", "unknown")   # return the user's unique ID


# =============================================================
#  USAGE LOGGING — record each generation in Supabase
#
#  Stores: user_id, action (resume/cover/ats), timestamp
#  This is how you later build:
#    • "You have used 2 of 3 free generations"
#    • Usage dashboard for yourself as the owner
#
#  Uses fire-and-forget pattern — if logging fails, the user
#  still gets their result. Never block on logging.
# =============================================================
async def log_usage(user_id: str, action: str):
    try:
        async with httpx.AsyncClient() as http:
            await http.post(
                f"{SUPABASE_URL}/rest/v1/usage_logs",
                headers={
                    "apikey": SUPABASE_ANON_KEY,
                    "Authorization": f"Bearer {SUPABASE_ANON_KEY}",
                    "Content-Type": "application/json",
                    "Prefer": "return=minimal",
                },
                json={"user_id": user_id, "action": action},
                timeout=5,
            )
    except Exception:
        pass   # never fail the main request because of a logging error


# =============================================================
#  HELPER — extract plain text from an uploaded file
# =============================================================
def extract_text(data: bytes, filename: str) -> str:
    ext = os.path.splitext(filename)[1].lower()
    if ext not in ALLOWED_EXT:
        raise ValueError(f"'{ext}' not supported. Upload PDF, DOCX, or TXT.")

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
        text = data.decode("utf-8", errors="replace")

    text = text.strip()
    if not text:
        raise ValueError("File is empty or has no extractable text.")
    return text


# =============================================================
#  HELPER — call OpenAI
# =============================================================
def ask_ai(system: str, user: str, max_tokens: int = 3500) -> str:
    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": system},
            {"role": "user",   "content": user},
        ],
        temperature=0.65,
        max_tokens=max_tokens,
    )
    return resp.choices[0].message.content.strip()


# =============================================================
#  HELPER — safely parse JSON from AI response
# =============================================================
def parse_json(raw: str) -> dict:
    clean = raw.strip()
    if clean.startswith("```"):
        parts = clean.split("```")
        clean = parts[1]
        if clean.lower().startswith("json"):
            clean = clean[4:]
    return json.loads(clean.strip())


# =============================================================
#  STEP 1 — ATS Pre-check (internal, silent)
# =============================================================
def run_ats_check(resume_text: str, job_description: str) -> dict:
    system = """You are an expert ATS analyst.
Analyse the resume against the job description.
Return ONLY valid JSON — no markdown, no explanation:
{
  "score":            <integer 0-100>,
  "level":            "<Excellent | Good | Fair | Poor>",
  "matched_keywords": ["keyword in resume"],
  "missing_keywords": ["keyword missing from resume"],
  "suggestions":      ["specific actionable improvement"]
}
Scoring: 90-100 Excellent, 70-89 Good, 50-69 Fair, 0-49 Poor"""

    user = (
        f"Resume:\n{resume_text}\n\n"
        f"Job Description:\n"
        f"{job_description or 'No job description — score on general best practices.'}"
    )
    try:
        return parse_json(ask_ai(system, user, max_tokens=1500))
    except Exception:
        return {
            "score": 50, "level": "Fair",
            "matched_keywords": [], "missing_keywords": [],
            "suggestions": ["Could not parse ATS analysis — proceeding with general optimisation."],
        }


# =============================================================
#  STEP 2 — ATS-Optimised Resume Rewrite
# =============================================================
def get_optimised_resume_data(resume_text: str, job_description: str, ats: dict) -> dict:
    missing = ", ".join(ats.get("missing_keywords", [])) or "none identified"
    matched = ", ".join(ats.get("matched_keywords", [])) or "none identified"
    score   = ats.get("score", 50)
    sugg    = "\n".join(f"- {s}" for s in ats.get("suggestions", []))

    system = f"""You are an expert resume writer and ATS optimisation specialist.

Current ATS score : {score}/100
Keywords matched  : {matched}
Keywords MISSING  : {missing}
Suggestions:
{sugg}

STRICT RULES:
1. Do NOT invent jobs, companies, dates, degrees, or qualifications.
2. Rephrase bullets to naturally include missing keywords where genuine.
3. Add missing keywords to Skills if legitimately related to existing skills.
4. Rewrite the Professional Summary to align with the target role.
5. Put the most relevant bullets first in each job.
6. Return [] for any section with no content.

Return ONLY valid JSON (no markdown):
{{
  "name": "Full Name",
  "phone": "phone or empty string",
  "email": "email or empty string",
  "linkedin": "linkedin URL or empty string",
  "summary": "3-4 sentence ATS-optimised summary",
  "skills": [{{"category": "Name", "items": "item1, item2"}}],
  "experience": [{{
    "title": "Job Title", "company": "Company, Location",
    "dates": "Month YYYY - Month YYYY",
    "bullets": ["bullet 1", "bullet 2"]
  }}],
  "education": [{{
    "institution": "School", "degree": "Degree",
    "dates": "YYYY - YYYY", "grade": "CGPA or empty string"
  }}],
  "certifications": [{{"name": "Cert Name", "year": "YYYY or empty string"}}],
  "projects": [{{
    "name": "Project", "tech": "Technologies",
    "bullets": ["what was built"]
  }}]
}}"""

    user = f"Original Resume:\n{resume_text}\n\nTarget Job Description:\n{job_description}"
    raw  = ask_ai(system, user, max_tokens=4000)
    try:
        return parse_json(raw)
    except json.JSONDecodeError:
        raise ValueError("AI returned unexpected data. Please try again.")


# =============================================================
#  STEP 2 (Cover) — ATS-Aware Cover Letter
# =============================================================
def get_optimised_cover_letter(resume_text: str, job_description: str, ats: dict) -> str:
    missing = ", ".join(ats.get("missing_keywords", [])) or "none"
    matched = ", ".join(ats.get("matched_keywords", [])) or "none"
    score   = ats.get("score", 50)

    system = f"""You are a world-class cover letter writer with deep ATS knowledge.
ATS score: {score}/100
Matched keywords: {matched}
Missing keywords: {missing}

Write a cover letter that:
1. Opens with a strong specific hook
2. Paragraph 1: highlights 2-3 matched experiences using matched keywords
3. Paragraph 2: bridges gaps using existing skills — do NOT fabricate skills
4. Strong closing with call to action
5. Max 380 words. Professional, genuine, direct.
6. Use the candidate's real name — no [placeholder] text."""

    user = f"Resume:\n{resume_text}\n\nJob Description:\n{job_description}"
    return ask_ai(system, user, max_tokens=1500)


# =============================================================
#  DOCX BUILDER HELPERS
# =============================================================
def _font(run, size=11, bold=False, italic=False):
    run.font.name   = "Calibri"
    run.font.size   = Pt(size)
    run.font.bold   = bold
    run.font.italic = italic

def _bottom_border(paragraph):
    pPr    = paragraph._p.get_or_add_pPr()
    pBdr   = OxmlElement("w:pBdr")
    bottom = OxmlElement("w:bottom")
    bottom.set(qn("w:val"),   "single")
    bottom.set(qn("w:sz"),    "6")
    bottom.set(qn("w:space"), "1")
    bottom.set(qn("w:color"), "000000")
    pBdr.append(bottom)
    pPr.append(pBdr)

def _section_heading(doc, title: str):
    p = doc.add_paragraph()
    p.paragraph_format.space_before = Pt(10)
    p.paragraph_format.space_after  = Pt(2)
    _font(p.add_run(title.upper()), size=13, bold=True)
    _bottom_border(p)

def _right_tab(paragraph):
    pPr  = paragraph._p.get_or_add_pPr()
    tabs = OxmlElement("w:tabs")
    tab  = OxmlElement("w:tab")
    tab.set(qn("w:val"), "right")
    tab.set(qn("w:pos"), "9360")
    tabs.append(tab)
    pPr.append(tabs)

def _build_doc_base() -> Document:
    doc = Document()
    for sec in doc.sections:
        sec.top_margin    = Inches(0.5)
        sec.bottom_margin = Inches(0.5)
        sec.left_margin   = Inches(0.5)
        sec.right_margin  = Inches(0.5)
    normal = doc.styles["Normal"]
    normal.font.name = "Calibri"
    normal.font.size = Pt(11)
    normal.paragraph_format.line_spacing = Pt(13)
    return doc

def _add_header(doc, d):
    p = doc.add_paragraph()
    p.paragraph_format.space_after = Pt(2)
    _font(p.add_run(d.get("name", "")), size=22, bold=True)
    parts = [x for x in [d.get("phone"), d.get("email"), d.get("linkedin")] if x and x.strip()]
    if parts:
        cp = doc.add_paragraph()
        cp.paragraph_format.space_after = Pt(6)
        _font(cp.add_run("  |  ".join(parts)), size=10, italic=True)

def _add_summary(doc, text):
    if not text or not text.strip(): return
    _section_heading(doc, "Professional Summary")
    p = doc.add_paragraph()
    p.paragraph_format.left_indent = Inches(0.15)
    p.paragraph_format.space_after = Pt(4)
    _font(p.add_run(text.strip()))

def _add_skills(doc, skills):
    valid = [s for s in (skills or []) if s.get("category","").strip() or s.get("items","").strip()]
    if not valid: return
    _section_heading(doc, "Technical Skills")
    for sk in valid:
        p = doc.add_paragraph()
        p.paragraph_format.left_indent = Inches(0.15)
        p.paragraph_format.space_after = Pt(1)
        _font(p.add_run(sk.get("category","") + ": "), bold=True)
        _font(p.add_run(sk.get("items","")))

def _add_experience(doc, experience):
    valid = [j for j in (experience or []) if j.get("title","").strip() or j.get("company","").strip()]
    if not valid: return
    _section_heading(doc, "Experience")
    for job in valid:
        p = doc.add_paragraph()
        p.paragraph_format.space_before = Pt(6)
        p.paragraph_format.space_after  = Pt(1)
        _font(p.add_run(job.get("title","")), bold=True)
        _font(p.add_run("  |  " + job.get("company","")))
        p.add_run("\t")
        _font(p.add_run(job.get("dates","")), size=10, italic=True)
        _right_tab(p)
        for b in job.get("bullets", []):
            if not b or not b.strip(): continue
            bp = doc.add_paragraph(style="List Bullet")
            bp.paragraph_format.left_indent = Inches(0.3)
            bp.paragraph_format.space_after = Pt(1)
            _font(bp.add_run(b))

def _add_projects(doc, projects):
    valid = [p for p in (projects or []) if p.get("name","").strip()]
    if not valid: return
    _section_heading(doc, "Projects")
    for proj in valid:
        p = doc.add_paragraph()
        p.paragraph_format.space_before = Pt(6)
        p.paragraph_format.space_after  = Pt(1)
        _font(p.add_run(proj.get("name","")), bold=True)
        if proj.get("tech","").strip():
            _font(p.add_run("  |  " + proj["tech"]), italic=True)
        for b in proj.get("bullets", []):
            if not b or not b.strip(): continue
            bp = doc.add_paragraph(style="List Bullet")
            bp.paragraph_format.left_indent = Inches(0.3)
            bp.paragraph_format.space_after = Pt(1)
            _font(bp.add_run(b))

def _add_certifications(doc, certs):
    valid = [c for c in (certs or []) if c.get("name","").strip()]
    if not valid: return
    _section_heading(doc, "Awards and Certifications")
    for c in valid:
        bp = doc.add_paragraph(style="List Bullet")
        bp.paragraph_format.left_indent = Inches(0.3)
        bp.paragraph_format.space_after = Pt(1)
        _font(bp.add_run(c.get("name","")), bold=True)
        if c.get("year","").strip():
            _font(bp.add_run("  |  " + c["year"]), italic=True)

def _add_education(doc, education):
    valid = [e for e in (education or []) if e.get("institution","").strip()]
    if not valid: return
    _section_heading(doc, "Education")
    for edu in valid:
        p = doc.add_paragraph()
        p.paragraph_format.left_indent = Inches(0.15)
        p.paragraph_format.space_after = Pt(2)
        _font(p.add_run(edu.get("institution","")), bold=True)
        if edu.get("grade","").strip():
            _font(p.add_run("  " + edu["grade"]), bold=True)
        _font(p.add_run("  •  " + edu.get("degree","")), italic=True)
        if edu.get("dates","").strip():
            _font(p.add_run("  •  " + edu["dates"]), size=10, italic=True)

def build_resume_docx(data: dict) -> bytes:
    doc = _build_doc_base()
    _add_header(doc, data)
    _add_summary(doc, data.get("summary",""))
    _add_skills(doc, data.get("skills",[]))
    _add_experience(doc, data.get("experience",[]))
    _add_projects(doc, data.get("projects",[]))
    _add_certifications(doc, data.get("certifications",[]))
    _add_education(doc, data.get("education",[]))
    buf = io.BytesIO()
    doc.save(buf)
    buf.seek(0)
    return buf.read()

def build_cover_letter_docx(cover_text: str) -> bytes:
    doc = Document()
    for sec in doc.sections:
        sec.top_margin = sec.bottom_margin = sec.left_margin = sec.right_margin = Inches(1.0)
    for line in cover_text.strip().split("\n"):
        line = line.strip()
        if not line:
            doc.add_paragraph()
            continue
        p = doc.add_paragraph()
        p.paragraph_format.space_after = Pt(8)
        _font(p.add_run(line))
    buf = io.BytesIO()
    doc.save(buf)
    buf.seek(0)
    return buf.read()


# =============================================================
#  PDF CONVERSION — LibreOffice (Render) → docx2pdf (local)
# =============================================================
def docx_to_pdf(docx_bytes: bytes):
    with tempfile.TemporaryDirectory() as tmp:
        docx_path = os.path.join(tmp, "resume.docx")
        pdf_path  = os.path.join(tmp, "resume.pdf")
        with open(docx_path, "wb") as f:
            f.write(docx_bytes)
        lo = shutil.which("soffice") or shutil.which("libreoffice")
        if lo:
            try:
                r = subprocess.run(
                    [lo, "--headless", "--convert-to", "pdf", "--outdir", tmp, docx_path],
                    capture_output=True, timeout=60
                )
                if r.returncode == 0 and os.path.exists(pdf_path):
                    return open(pdf_path, "rb").read()
            except Exception:
                pass
        try:
            from docx2pdf import convert
            convert(docx_path, pdf_path)
            if os.path.exists(pdf_path):
                return open(pdf_path, "rb").read()
        except Exception:
            pass
    return None


# =============================================================
#  HELPER — save bytes to temp file → FileResponse
# =============================================================
def temp_response(data, suffix, media, filename, extra_headers=None):
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    tmp.write(data)
    tmp.close()
    return FileResponse(path=tmp.name, media_type=media,
                        filename=filename, headers=extra_headers or {})


# =============================================================
#  ENDPOINT  GET /   — health check (no auth needed)
# =============================================================
@app.get("/")
def root():
    return {"status": "ok", "message": "AI Resume Builder v4 is running ✅"}


# =============================================================
#  ENDPOINT  POST /upload  (auth required)
# =============================================================
@app.post("/upload")
async def upload(request: Request, file: UploadFile = File(...)):
    await get_current_user(request)   # verify login — raises 401 if not logged in
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
#  ENDPOINT  POST /generate  (auth required)
# =============================================================
@app.post("/generate")
async def generate(
    request:         Request,
    file:            UploadFile = File(...),
    job_description: str        = Form(default=""),
    option:          str        = Form(default="resume"),
    output_format:   str        = Form(default="docx"),
):
    # ── 1. Verify the user is logged in ───────────────────────
    user_id = await get_current_user(request)

    # ── 2. Validate inputs ────────────────────────────────────
    if option not in {"resume", "cover", "ats"}:
        raise HTTPException(400, f"Unknown option '{option}'.")
    if output_format not in {"docx", "pdf", "txt"}:
        raise HTTPException(400, f"Unknown format '{output_format}'.")
    if not file or not file.filename:
        raise HTTPException(400, "Please attach your resume file.")

    raw_data = await file.read()
    if not raw_data:
        raise HTTPException(400, "Uploaded file is empty.")
    try:
        resume_text = extract_text(raw_data, file.filename)
    except ValueError as e:
        raise HTTPException(422, str(e))

    # ── 3. Log the usage (fire and forget) ────────────────────
    await log_usage(user_id, option)

    # =========================================================
    #  MODE 1 — ATS SCORE
    # =========================================================
    if option == "ats":
        try:
            result = run_ats_check(resume_text, job_description)
        except Exception as e:
            raise HTTPException(500, f"ATS check error: {e}")
        return JSONResponse(content=result)

    # =========================================================
    #  MODE 2 — COVER LETTER
    # =========================================================
    if option == "cover":
        if not job_description.strip():
            raise HTTPException(400, "A job description is required for a cover letter.")
        try:
            ats        = run_ats_check(resume_text, job_description)
            cover_text = get_optimised_cover_letter(resume_text, job_description, ats)
        except Exception as e:
            raise HTTPException(500, f"Error generating cover letter: {e}")

        if output_format == "txt":
            return temp_response(cover_text.encode(), ".txt", "text/plain", "cover_letter.txt")

        docx_bytes = build_cover_letter_docx(cover_text)
        if output_format == "docx":
            return temp_response(docx_bytes, ".docx", DOCX_MIME, "cover_letter.docx")
        if output_format == "pdf":
            pdf = docx_to_pdf(docx_bytes)
            if pdf:
                return temp_response(pdf, ".pdf", "application/pdf", "cover_letter.pdf")
            return temp_response(docx_bytes, ".docx", DOCX_MIME, "cover_letter.docx",
                                 {"X-Fallback": "pdf-unavailable"})

    # =========================================================
    #  MODE 3 — RESUME
    # =========================================================
    if option == "resume":
        try:
            ats  = run_ats_check(resume_text, job_description)
            data = get_optimised_resume_data(resume_text, job_description, ats)
        except ValueError as e:
            raise HTTPException(422, str(e))
        except Exception as e:
            raise HTTPException(500, f"Error optimising resume: {e}")

        if output_format == "txt":
            system_txt = (
                "Rewrite as clean plain text. Headers in ALL CAPS with dashes underneath. "
                "Bullet points start with '- '. No markdown, no LaTeX."
            )
            txt = ask_ai(system_txt, resume_text)
            return temp_response(txt.encode(), ".txt", "text/plain", "resume.txt")

        try:
            docx_bytes = build_resume_docx(data)
        except Exception as e:
            raise HTTPException(500, f"Error building Word document: {e}")

        if output_format == "docx":
            return temp_response(docx_bytes, ".docx", DOCX_MIME, "resume.docx")
        if output_format == "pdf":
            pdf = docx_to_pdf(docx_bytes)
            if pdf:
                return temp_response(pdf, ".pdf", "application/pdf", "resume.pdf")
            return temp_response(docx_bytes, ".docx", DOCX_MIME, "resume.docx",
                                 {"X-Fallback": "pdf-unavailable"})
