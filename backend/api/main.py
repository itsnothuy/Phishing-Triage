"""Main FastAPI application for phishing triage service."""
from fastapi import FastAPI, UploadFile, File, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from typing import Optional
import uuid
import os
from datetime import datetime
from dotenv import load_dotenv

# Load .env file from the project root
# This ensures that OPENAI_API_KEY is available as an environment variable
load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", ".env"))

from .models import init_db, get_db, Submission
from .schemas import SubmitURL, SubmissionResponse, ReportResponse, HealthResponse, MetricsResponse
from .pipeline import handle_url_submission, handle_eml_submission

# Initialize FastAPI app
app = FastAPI(
    title="Phish Triage",
    version="0.1.0",
    description="Automated phishing detection and enrichment service"
)

# Add CORS middleware for frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000", "http://localhost:8000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Initialize database on startup
@app.on_event("startup")
async def startup_event():
    """Initialize database tables on startup."""
    init_db()


@app.get("/health", response_model=HealthResponse)
def health():
    """Health check endpoint."""
    return HealthResponse(
        status="ok",
        timestamp=datetime.utcnow(),
        version="0.1.0"
    )


@app.post("/submit-url", response_model=SubmissionResponse)
async def submit_url_endpoint(
    url_req: SubmitURL,
    db: Session = Depends(get_db)
):
    """Submit a URL for analysis."""
    submission_id = str(uuid.uuid4())
    submission = Submission(
        id=submission_id,
        submission_type="url",
        url=str(url_req.url),
        status="queued",
        detonate=url_req.detonate,
        sandbox_provider=url_req.provider
    )
    
    db.add(submission)
    db.commit()
    
    # Process submission
    try:
        result = await handle_url_submission(submission_id, url_req.model_dump(), db)
        
        # Update submission with results
        submission.status = result.get("status", "completed")
        submission.score = result.get("score")
        submission.report_markdown = result.get("report_markdown")
        submission.enrichment = result.get("enrichment")
        submission.sandbox_data = result.get("sandbox")
        submission.iocs = result.get("iocs")
        submission.features = result.get("features")
        
        db.commit()
        
    except Exception as e:
        submission.status = "failed"
        db.commit()
        raise HTTPException(500, f"Processing failed: {str(e)}")
    
    return SubmissionResponse(
        id=submission_id,
        status=submission.status,
        created_at=submission.created_at
    )


@app.post("/submit", response_model=SubmissionResponse)
async def submit(
    url_req: Optional[SubmitURL] = None,
    eml: Optional[UploadFile] = File(None),
    db: Session = Depends(get_db)
):
    """Submit a URL or email file for analysis (legacy endpoint)."""
    if url_req:
        return await submit_url_endpoint(url_req, db)
    elif eml:
        return await submit_email_endpoint(eml, db)
    else:
        raise HTTPException(400, "Provide url JSON or upload .eml file")


@app.post("/submit-email", response_model=SubmissionResponse)
async def submit_email_endpoint(
    eml: UploadFile = File(...),
    db: Session = Depends(get_db)
):
    """Submit an email file for analysis."""
    submission_id = str(uuid.uuid4())
    content = await eml.read()
    
    submission = Submission(
        id=submission_id,
        submission_type="email",
        email_content=content.decode('utf-8', errors='ignore'),
        status="queued"
    )
    
    db.add(submission)
    db.commit()
    
    # Process submission
    try:
        result = await handle_eml_submission(submission_id, db)
        
        # Update submission with results
        submission.status = result.get("status", "completed")
        submission.score = result.get("score")
        submission.report_markdown = result.get("report_markdown")
        submission.enrichment = result.get("enrichment")
        submission.sandbox_data = result.get("sandbox")
        submission.iocs = result.get("iocs")
        
        db.commit()
        
    except Exception as e:
        submission.status = "failed"
        db.commit()
        raise HTTPException(500, f"Processing failed: {str(e)}")
    
    return SubmissionResponse(
        id=submission_id,
        status=submission.status,
        created_at=submission.created_at
    )





@app.get("/report/{submission_id}", response_model=ReportResponse)
def report(submission_id: str, db: Session = Depends(get_db)):
    """Get analysis report for a submission."""
    submission = db.query(Submission).filter(Submission.id == submission_id).first()
    
    if not submission:
        raise HTTPException(404, "Submission not found")
    
    return ReportResponse(
        id=submission.id,
        status=submission.status,
        score=submission.score,
        report_markdown=submission.report_markdown,
        enrichment=submission.enrichment,
        sandbox=submission.sandbox_data,
        iocs=submission.iocs,
        created_at=submission.created_at,
        updated_at=submission.updated_at
    )


@app.get("/metrics", response_model=MetricsResponse)
def metrics(db: Session = Depends(get_db)):
    """Get service metrics."""
    from datetime import timedelta
    from sqlalchemy import func
    
    total = db.query(func.count(Submission.id)).scalar()
    
    # Last 24h submissions
    yesterday = datetime.utcnow() - timedelta(days=1)
    recent = db.query(func.count(Submission.id)).filter(
        Submission.created_at >= yesterday
    ).scalar()
    
    # Average score
    avg_score = db.query(func.avg(Submission.score)).filter(
        Submission.score.isnot(None)
    ).scalar() or 0.0
    
    # High risk count (score >= threshold)
    threshold = float(os.getenv("RISK_THRESHOLD", "0.85"))
    high_risk = db.query(func.count(Submission.id)).filter(
        Submission.score >= threshold
    ).scalar()
    
    # Detonations today
    today = datetime.utcnow().date()
    detonations = db.query(func.count(Submission.id)).filter(
        Submission.detonate == True,
        func.date(Submission.created_at) == today
    ).scalar()
    
    return MetricsResponse(
        total_submissions=total,
        submissions_last_24h=recent,
        average_score=float(avg_score),
        high_risk_count=high_risk,
        detonations_today=detonations,
        model_version="1.0.0",
        last_drift_check=None,  # Will be implemented with drift detection
        drift_detected=False
    )


@app.post("/intel")
async def threat_intel(url_data: dict):
    """Get threat intelligence for a URL without full analysis."""
    url = url_data.get("url")
    if not url:
        raise HTTPException(400, "URL required")
    
    try:
        from enrich.advanced_intel import ThreatIntelAggregator
        aggregator = ThreatIntelAggregator()
        result = aggregator.analyze_url(url, enable_all=True)
        return result
    except Exception as e:
        raise HTTPException(500, f"Intelligence lookup failed: {str(e)}")


@app.get("/")
def root():
    """Root endpoint with API information."""
    return {
        "service": "Phish Triage API",
        "version": "0.1.0",
        "docs": "/docs",
        "health": "/health",
        "endpoints": {
            "submit_url": "/submit-url",
            "submit_email": "/submit-email", 
            "threat_intel": "/intel",
            "report": "/report/{id}",
            "metrics": "/metrics"
        }
    }
