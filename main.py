# =============================================================
# main.py  —  AI Resume Builder  (FastAPI Backend)
# =============================================================
#
#  FLOW FOR RESUME MODE (plain English):
#  ──────────────────────────────────────
#  Step 1 — ATS Pre-check  (internal, silent)
#            AI reads the resume + job description and returns:
#            a score, the missing keywords, and matched ones.
#
#  Step 2 — ATS-Optimised Rewrite
#            AI rewrites the resume using:
#              • Original resume facts (nothing invented)
#              • Missing keywords woven naturally into bullets
#              • Summary and skills tuned to the job description
#            Goal: push ATS score as high as possible while the
#            content still sounds like the real person.
#
#  Step 3 — Build output file  (DOCX, PDF, or TXT)
#
#  FLOW FOR COVER LETTER MODE:
#  ────────────────────────────
#  Same ATS pre-check → write a letter that uses the matched
#  keywords and addresses the missing ones naturally.
#
#  EXPORT FORMATS:
#  ────────────────
#   docx  — editable Word document  (always works, no extra tools)
#   pdf   — real PDF via docx2pdf   (uses MS Word / LibreOffice on
#            the machine; falls back to DOCX with a note if unavailable)
#   txt   — plain text file
#
#  ENDPOINTS:
#  ──────────
#   GET  /          — health check
#   POST /upload    — extract text preview from uploaded file
#   POST /generate  — main AI endpoint  (resume | cover | ats)
#
# =============================================================

import os, io, json, tempfile, traceback
from fastapi import FastAPI, File, UploadFile, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse

from dotenv import load_dotenv
load_dotenv()

import PyPDF2
from docx import Document
from docx.shared import Pt, Inches
from docx.oxml.ns import qn
from docx.oxml import OxmlElement
from openai import OpenAI

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

app = FastAPI(title="AI Resume Builder", version="3.0.0")

# CORS — which websites are allowed to call this API
# Locally:      allow everything (*)
# On Render:    only allow your Vercel frontend URL
# Set ALLOWED_ORIGINS in Render environment variables like:
#   https://your-app.vercel.app,https://yourcustomdomain.com
_raw_origins = os.getenv("ALLOWED_ORIGINS", "*")
_origins = [o.strip() for o in _raw_origins.split(",")] if _raw_origins != "*" else ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

ALLOWED_EXT    = {".pdf", ".docx", ".txt"}
DOCX_MIME      = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


# =============================================================
#  HELPER — extract plain text from an uploaded file
# =============================================================
def extract_text(data: bytes, filename: str) -> str:
    """Reads PDF / DOCX / TXT bytes and returns plain text."""
    ext = os.path.splitext(filename)[1].lower()
    if ext not in ALLOWED_EXT:
        raise ValueError(f"'{ext}' is not supported. Upload PDF, DOCX, or TXT.")

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
        raise ValueError("The file appears to be empty or has no extractable text.")
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
#  HELPER — safely parse JSON from an AI response
#  The AI sometimes wraps JSON in ```json ... ``` fences.
#  This strips those out before parsing.
# =============================================================
def parse_json(raw: str) -> dict:
    clean = raw.strip()
    if clean.startswith("```"):
        parts = clean.split("```")
        # parts[1] is the content between the first pair of fences
        clean = parts[1]
        if clean.lower().startswith("json"):
            clean = clean[4:]
    return json.loads(clean.strip())


# =============================================================
#  STEP 1 — ATS Pre-check  (internal helper, used by all modes)
#
#  Returns a dict:
#  {
#    "score":            int,
#    "level":            str,   "Excellent" | "Good" | "Fair" | "Poor"
#    "matched_keywords": list,
#    "missing_keywords": list,
#    "suggestions":      list
#  }
#
#  This is called silently before rewriting the resume/cover letter
#  so the AI knows exactly what gaps to fill.
# =============================================================
def run_ats_check(resume_text: str, job_description: str) -> dict:
    system = """You are an expert ATS (Applicant Tracking System) analyst.

Analyse the resume against the job description.
Return ONLY valid JSON — no markdown, no explanation:

{
  "score":            <integer 0-100>,
  "level":            "<Excellent | Good | Fair | Poor>",
  "matched_keywords": ["keyword that IS in the resume"],
  "missing_keywords": ["keyword that is NOT in resume but required by job"],
  "suggestions":      ["specific actionable improvement"]
}

Scoring guide:
  90-100  Excellent  — very strong alignment
  70-89   Good       — solid match, minor gaps
  50-69   Fair       — noticeable keyword and skills gaps
  0-49    Poor       — significant mismatch
"""
    user = (
        f"Resume:\n{resume_text}\n\n"
        f"Job Description:\n"
        f"{job_description or 'No job description provided — score against general best practices.'}"
    )
    try:
        raw    = ask_ai(system, user, max_tokens=1500)
        return parse_json(raw)
    except Exception:
        # If parsing fails, return a safe default so the rest of the flow continues
        return {
            "score": 50, "level": "Fair",
            "matched_keywords": [],
            "missing_keywords": [],
            "suggestions":      ["Could not parse ATS analysis — proceeding with general optimisation."],
        }


# =============================================================
#  STEP 2 — ATS-Optimised Resume Rewrite
#
#  Takes:
#    resume_text    — original resume plain text
#    job_description — the job posting
#    ats            — the result from run_ats_check()
#
#  Returns:
#    A dict with the structured resume data, ready for DOCX building.
#
#  Key rule: the AI MUST NOT invent facts. It can:
#    • Rephrase bullets to include missing keywords naturally
#    • Add the missing keywords to the Skills section if genuinely relevant
#    • Rewrite the summary to align with the role
#    • Reorder bullets to put the most relevant ones first
# =============================================================
def get_optimised_resume_data(resume_text: str, job_description: str, ats: dict) -> dict:
    missing  = ", ".join(ats.get("missing_keywords", [])) or "none identified"
    matched  = ", ".join(ats.get("matched_keywords", [])) or "none identified"
    score    = ats.get("score", 50)
    sugg     = "\n".join(f"- {s}" for s in ats.get("suggestions", []))

    system = f"""You are an expert resume writer and ATS optimisation specialist.

The candidate's resume has been pre-scored against the job description:
  Current ATS score : {score}/100
  Keywords matched  : {matched}
  Keywords MISSING  : {missing}

ATS suggestions:
{sugg}

YOUR TASK:
Rewrite the resume to maximise the ATS score while keeping it 100% truthful.

STRICT RULES — read carefully:
1. Do NOT invent jobs, companies, dates, degrees, or qualifications.
2. You MAY rephrase experience bullets to naturally include missing keywords
   if those keywords genuinely describe what the person did.
3. You MAY add missing keywords to the Skills section if they are
   legitimately related to the person's existing skillset.
4. Rewrite the Professional Summary to align with the target role.
5. Prioritise the most relevant bullets in each job — put the best ones first.
6. Keep every bullet concise (1-2 lines), action-verb led, with metrics where possible.
7. If a section has no content, return an empty list [].

Return ONLY valid JSON (no markdown fences, no commentary):

{{
  "name":        "Full Name",
  "phone":       "phone number or empty string",
  "email":       "email or empty string",
  "linkedin":    "linkedin URL or empty string",
  "summary":     "3-4 sentence ATS-optimised summary targeting this specific role",
  "skills": [
    {{"category": "Category Name", "items": "item1, item2, item3"}}
  ],
  "experience": [
    {{
      "title":   "Job Title",
      "company": "Company, Location",
      "dates":   "Month YYYY - Month YYYY",
      "bullets": ["ATS-optimised bullet 1", "bullet 2"]
    }}
  ],
  "education": [
    {{
      "institution": "School Name",
      "degree":      "Degree",
      "dates":       "YYYY - YYYY",
      "grade":       "CGPA or empty string"
    }}
  ],
  "certifications": [
    {{"name": "Certification Name", "year": "YYYY or empty string"}}
  ],
  "projects": [
    {{
      "name":    "Project Name",
      "tech":    "Technologies",
      "bullets": ["What was built / achieved"]
    }}
  ]
}}
"""
    user = (
        f"Original Resume:\n{resume_text}\n\n"
        f"Target Job Description:\n{job_description}"
    )
    raw = ask_ai(system, user, max_tokens=4000)
    try:
        return parse_json(raw)
    except json.JSONDecodeError:
        raise ValueError("AI returned unexpected data during optimisation. Please try again.")


# =============================================================
#  STEP 2 (Cover Letter) — ATS-Aware Cover Letter
#
#  Uses the ATS gap analysis to write a letter that:
#    • Opens with a hook connecting the candidate to the role
#    • Paragraph 1: highlights matched experience + keywords
#    • Paragraph 2: bridges gaps (missing keywords) with real examples
#    • Closes with a confident call to action
# =============================================================
def get_optimised_cover_letter(resume_text: str, job_description: str, ats: dict) -> str:
    missing = ", ".join(ats.get("missing_keywords", [])) or "none identified"
    matched = ", ".join(ats.get("matched_keywords", [])) or "none identified"
    score   = ats.get("score", 50)

    system = f"""You are a world-class cover letter writer with deep knowledge of ATS systems.

The candidate's resume scores {score}/100 against the job description.
  Keywords already matched : {matched}
  Keywords currently missing: {missing}

Write a compelling cover letter that:
1. Opens with a strong, specific hook (no generic "I am applying for...")
2. Paragraph 1: highlights 2-3 most relevant matched experiences using matched keywords
3. Paragraph 2: naturally bridges the gap by referencing how existing skills relate
   to the missing keywords — do NOT pretend skills the candidate doesn't have
4. Closing: confident, specific, with a clear call to action
5. Max 380 words. Tone: professional, genuine, direct.
6. Use the candidate's real name from the resume — no placeholders like [Your Name].

Do NOT include placeholder brackets of any kind.
Write the full letter text only — no subject line, no metadata.
"""
    user = (
        f"Original Resume:\n{resume_text}\n\n"
        f"Target Job Description:\n{job_description}"
    )
    return ask_ai(system, user, max_tokens=1500)


# =============================================================
#  DOCX BUILDER HELPERS
#  These functions build the Word document styled to match the
#  LaTeX template the user provided:
#    • 0.5 inch margins  (same as \usepackage[margin=0.5in]{geometry})
#    • Name in large bold  (same as \Huge \textbf{Name})
#    • Contact line italic (same as \textit{phone} $|$ \textit{email})
#    • Section headings ALL CAPS + bottom border  (same as \titlerule)
#    • Skills: bold category + items  (same as \titleItem)
#    • Experience: bold title | company, italic date  (\resumeProjectHeading)
#    • Bullet points indented  (\resumeItem)
# =============================================================

def _font(run, size=11, bold=False, italic=False):
    """Quick helper to set font on a run."""
    run.font.name   = "Calibri"
    run.font.size   = Pt(size)
    run.font.bold   = bold
    run.font.italic = italic


def _bottom_border(paragraph):
    """
    Adds a solid bottom border under a paragraph.
    Replicates the \\titlerule horizontal line from the LaTeX template.
    """
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
    """ALL CAPS heading with bottom rule — mirrors LaTeX \\section{}."""
    p = doc.add_paragraph()
    p.paragraph_format.space_before = Pt(10)
    p.paragraph_format.space_after  = Pt(2)
    _font(p.add_run(title.upper()), size=13, bold=True)
    _bottom_border(p)


def _right_tab(paragraph):
    """Sets a right-aligned tab stop at the right margin (~6.5 inches)."""
    pPr  = paragraph._p.get_or_add_pPr()
    tabs = OxmlElement("w:tabs")
    tab  = OxmlElement("w:tab")
    tab.set(qn("w:val"), "right")
    tab.set(qn("w:pos"), "9360")   # 9360 twips = 6.5 inches
    tabs.append(tab)
    pPr.append(tabs)


def _build_doc_base() -> Document:
    """Creates a Document with the standard 0.5-inch margins."""
    doc = Document()
    for sec in doc.sections:
        sec.top_margin    = Inches(0.5)
        sec.bottom_margin = Inches(0.5)
        sec.left_margin   = Inches(0.5)
        sec.right_margin  = Inches(0.5)
    # Default style
    normal = doc.styles["Normal"]
    normal.font.name = "Calibri"
    normal.font.size = Pt(11)
    normal.paragraph_format.line_spacing = Pt(13)
    return doc


# ── Individual section builders ───────────────────────────────

def _add_header(doc, d: dict):
    """Name + contact line."""
    p = doc.add_paragraph()
    p.paragraph_format.space_after = Pt(2)
    _font(p.add_run(d.get("name", "")), size=22, bold=True)

    parts = [x for x in [d.get("phone"), d.get("email"), d.get("linkedin")] if x and x.strip()]
    if parts:
        cp = doc.add_paragraph()
        cp.paragraph_format.space_after = Pt(6)
        _font(cp.add_run("  |  ".join(parts)), size=10, italic=True)


def _add_summary(doc, text: str):
    if not text or not text.strip():
        return
    _section_heading(doc, "Professional Summary")
    p = doc.add_paragraph()
    p.paragraph_format.left_indent = Inches(0.15)
    p.paragraph_format.space_after = Pt(4)
    _font(p.add_run(text.strip()))


def _add_skills(doc, skills: list):
    if not skills:
        return
    # Filter out blank entries
    valid = [s for s in skills if s.get("category","").strip() or s.get("items","").strip()]
    if not valid:
        return
    _section_heading(doc, "Technical Skills")
    for sk in valid:
        p = doc.add_paragraph()
        p.paragraph_format.left_indent = Inches(0.15)
        p.paragraph_format.space_after = Pt(1)
        _font(p.add_run(sk.get("category","") + ": "), bold=True)
        _font(p.add_run(sk.get("items","")))


def _add_experience(doc, experience: list):
    if not experience:
        return
    valid = [j for j in experience if j.get("title","").strip() or j.get("company","").strip()]
    if not valid:
        return
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
            if not b or not b.strip():
                continue
            bp = doc.add_paragraph(style="List Bullet")
            bp.paragraph_format.left_indent = Inches(0.3)
            bp.paragraph_format.space_after = Pt(1)
            _font(bp.add_run(b))


def _add_projects(doc, projects: list):
    valid = [p for p in (projects or []) if p.get("name","").strip()]
    if not valid:
        return
    _section_heading(doc, "Projects")
    for proj in valid:
        p = doc.add_paragraph()
        p.paragraph_format.space_before = Pt(6)
        p.paragraph_format.space_after  = Pt(1)
        _font(p.add_run(proj.get("name","")), bold=True)
        if proj.get("tech","").strip():
            _font(p.add_run("  |  " + proj["tech"]), italic=True)
        for b in proj.get("bullets", []):
            if not b or not b.strip():
                continue
            bp = doc.add_paragraph(style="List Bullet")
            bp.paragraph_format.left_indent = Inches(0.3)
            bp.paragraph_format.space_after = Pt(1)
            _font(bp.add_run(b))


def _add_certifications(doc, certs: list):
    # Filter blanks first — this prevents the empty-heading bug
    valid = [c for c in (certs or []) if c.get("name","").strip()]
    if not valid:
        return
    _section_heading(doc, "Awards and Certifications")
    for c in valid:
        bp = doc.add_paragraph(style="List Bullet")
        bp.paragraph_format.left_indent = Inches(0.3)
        bp.paragraph_format.space_after = Pt(1)
        _font(bp.add_run(c.get("name","")), bold=True)
        if c.get("year","").strip():
            _font(bp.add_run("  |  " + c["year"]), italic=True)


def _add_education(doc, education: list):
    valid = [e for e in (education or []) if e.get("institution","").strip()]
    if not valid:
        return
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


# =============================================================
#  Build resume DOCX bytes from structured data dict
# =============================================================
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


# =============================================================
#  Build cover letter DOCX bytes from plain text
# =============================================================
def build_cover_letter_docx(cover_text: str) -> bytes:
    doc = Document()
    for sec in doc.sections:
        sec.top_margin    = Inches(1.0)
        sec.bottom_margin = Inches(1.0)
        sec.left_margin   = Inches(1.0)
        sec.right_margin  = Inches(1.0)
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
#  Convert DOCX bytes → PDF bytes
#
#  Strategy (tries each in order, returns first that works):
#
#  1. LibreOffice  — available on Render's Linux servers (free).
#                    Installed via render.yaml build command.
#  2. docx2pdf     — works on Windows/Mac locally using MS Word.
#  3. None         — both failed; caller falls back to DOCX file.
#
#  NEVER raises — failures are handled silently.
# =============================================================
def docx_to_pdf(docx_bytes: bytes) -> bytes | None:
    import subprocess, shutil

    with tempfile.TemporaryDirectory() as tmp:
        docx_path = os.path.join(tmp, "resume.docx")
        pdf_path  = os.path.join(tmp, "resume.pdf")

        with open(docx_path, "wb") as f:
            f.write(docx_bytes)

        # ── Try 1: LibreOffice (works on Render Linux) ──────
        # soffice is the LibreOffice command-line binary
        lo = shutil.which("soffice") or shutil.which("libreoffice")
        if lo:
            try:
                result = subprocess.run(
                    [lo, "--headless", "--convert-to", "pdf",
                     "--outdir", tmp, docx_path],
                    capture_output=True, timeout=60
                )
                if result.returncode == 0 and os.path.exists(pdf_path):
                    with open(pdf_path, "rb") as f:
                        return f.read()
            except Exception:
                pass

        # ── Try 2: docx2pdf (works locally on Windows/Mac) ──
        try:
            from docx2pdf import convert
            convert(docx_path, pdf_path)
            if os.path.exists(pdf_path):
                with open(pdf_path, "rb") as f:
                    return f.read()
        except Exception:
            pass

    # Both methods failed
    return None


# =============================================================
#  Save bytes to a temp file → return FileResponse
# =============================================================
def temp_response(data: bytes, suffix: str, media: str, filename: str,
                  extra_headers: dict = None) -> FileResponse:
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    tmp.write(data)
    tmp.close()
    return FileResponse(path=tmp.name, media_type=media,
                        filename=filename, headers=extra_headers or {})


# =============================================================
#  ENDPOINT  GET /
# =============================================================
@app.get("/")
def root():
    return {"status": "ok", "message": "AI Resume Builder v3 is running ✅"}


# =============================================================
#  ENDPOINT  POST /upload
#  Preview extracted text from an uploaded resume file
# =============================================================
@app.post("/upload")
async def upload(file: UploadFile = File(...)):
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
#
#  option        : "resume" | "cover" | "ats"
#  output_format : "docx"   | "pdf"   | "txt"
# =============================================================
@app.post("/generate")
async def generate(
    file:            UploadFile = File(...),
    job_description: str        = Form(default=""),
    option:          str        = Form(default="resume"),
    output_format:   str        = Form(default="docx"),
):
    # ── Validate inputs ───────────────────────────────────────
    if option not in {"resume", "cover", "ats"}:
        raise HTTPException(400, f"Unknown option '{option}'. Use: resume | cover | ats")
    if output_format not in {"docx", "pdf", "txt"}:
        raise HTTPException(400, f"Unknown format '{output_format}'. Use: docx | pdf | txt")
    if not file or not file.filename:
        raise HTTPException(400, "Please attach your resume file.")

    raw_data = await file.read()
    if not raw_data:
        raise HTTPException(400, "Uploaded file is empty.")

    try:
        resume_text = extract_text(raw_data, file.filename)
    except ValueError as e:
        raise HTTPException(422, str(e))

    # =========================================================
    #  MODE 1 — ATS SCORE  (always returns JSON to the browser)
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
            raise HTTPException(
                400,
                "A job description is required to generate a cover letter. "
                "Please paste the job posting into the Job Description field."
            )
        try:
            # Step 1: silent ATS pre-check to find gaps
            ats = run_ats_check(resume_text, job_description)

            # Step 2: write an ATS-aware cover letter
            cover_text = get_optimised_cover_letter(resume_text, job_description, ats)
        except Exception as e:
            raise HTTPException(500, f"Error generating cover letter: {e}")

        if output_format == "txt":
            return temp_response(
                cover_text.encode("utf-8"), ".txt", "text/plain", "cover_letter.txt"
            )

        # Build DOCX first (needed for both docx and pdf outputs)
        docx_bytes = build_cover_letter_docx(cover_text)

        if output_format == "docx":
            return temp_response(docx_bytes, ".docx", DOCX_MIME, "cover_letter.docx")

        if output_format == "pdf":
            pdf_bytes = docx_to_pdf(docx_bytes)
            if pdf_bytes:
                return temp_response(pdf_bytes, ".pdf", "application/pdf", "cover_letter.pdf")
            else:
                # PDF conversion unavailable — return DOCX and tell the user
                return temp_response(
                    docx_bytes, ".docx", DOCX_MIME, "cover_letter.docx",
                    {"X-Fallback": "pdf-unavailable"}
                )

    # =========================================================
    #  MODE 3 — RESUME  (the main flow)
    #
    #  Full pipeline:
    #    1. ATS pre-check   → find score + missing keywords
    #    2. ATS-optimised rewrite  → structured resume data
    #    3. Build output file (DOCX / PDF / TXT)
    # =========================================================
    if option == "resume":
        try:
            # ── Step 1: ATS pre-check ──────────────────────────
            ats  = run_ats_check(resume_text, job_description)

            # ── Step 2: ATS-aware optimised rewrite ────────────
            data = get_optimised_resume_data(resume_text, job_description, ats)

        except ValueError as e:
            raise HTTPException(422, str(e))
        except Exception as e:
            raise HTTPException(500, f"Error optimising resume: {e}")

        # ── TXT output ─────────────────────────────────────────
        if output_format == "txt":
            system_txt = (
                "Rewrite the resume as clean plain text. "
                "Section headers in ALL CAPS followed by a line of dashes. "
                "Bullet points start with '- '. No markdown, no LaTeX. "
                "Keep all facts exactly as given."
            )
            try:
                txt = ask_ai(system_txt, resume_text)
            except Exception as e:
                raise HTTPException(500, f"OpenAI error: {e}")
            return temp_response(txt.encode("utf-8"), ".txt", "text/plain", "resume.txt")

        # ── Build DOCX (used for both docx and pdf) ────────────
        try:
            docx_bytes = build_resume_docx(data)
        except Exception as e:
            raise HTTPException(500, f"Error building Word document: {e}")

        if output_format == "docx":
            return temp_response(docx_bytes, ".docx", DOCX_MIME, "resume.docx")

        if output_format == "pdf":
            pdf_bytes = docx_to_pdf(docx_bytes)
            if pdf_bytes:
                return temp_response(pdf_bytes, ".pdf", "application/pdf", "resume.pdf")
            else:
                # Microsoft Word / LibreOffice not found on server
                # Return the DOCX anyway and let the frontend know
                return temp_response(
                    docx_bytes, ".docx", DOCX_MIME, "resume.docx",
                    {"X-Fallback": "pdf-unavailable"}
                )
