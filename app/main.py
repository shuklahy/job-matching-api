import os
import json
import uuid
import time
import logging

from contextlib import asynccontextmanager

import psycopg
from redis import Redis
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from prometheus_client import Counter, make_asgi_app

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

log = logging.getLogger("profile-api")

DB = os.environ["DATABASE_URL"]
QUEUE = os.environ.get("REDIS_URL", "redis://redis:6379/0")
REQ = Counter(
    "profile_api_requests_total",
    "API calls",
    ["endpoint"]
)

def connect():
    return psycopg.connect(DB)


# ============================================================
# Database Initialization
# ============================================================

def initialize():
    for attempt in range(30):
        try:
            with connect() as conn:

                # Create profiles table
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS profiles (
                        id TEXT PRIMARY KEY,
                        name TEXT NOT NULL,
                        skills JSONB NOT NULL,
                        status TEXT NOT NULL,
                        created_at TIMESTAMPTZ DEFAULT NOW()
                    )
                """)

                # Create jobs table
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS jobs (
                        id TEXT PRIMARY KEY,
                        profile_id TEXT NOT NULL REFERENCES profiles(id),
                        status TEXT NOT NULL,
                        created_at TIMESTAMPTZ DEFAULT NOW()
                    )
                """)

                # Create positions table
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS positions (
                        id SERIAL PRIMARY KEY,
                        title TEXT UNIQUE NOT NULL,
                        skills JSONB NOT NULL
                    )
                """)

                # Default job positions and required skills
                positions = [
                    (
                        "Python Backend Engineer",
                        ["python", "postgresql", "redis", "docker"]
                    ),
                    (
                        "DevOps Engineer",
                        ["docker", "nginx", "prometheus", "grafana"]
                    ),
                    (
                        "Data Engineer",
                        ["python", "sql", "etl", "postgresql"]
                    ),
                    (
                        "Platform Engineer",
                        ["kubernetes", "docker", "python", "linux"]
                    ),
                    (
                        "Observability Engineer",
                        ["prometheus", "grafana", "elasticsearch", "linux"]
                    )
                ]

                # Insert default positions if they do not exist
                for title, skills in positions:
                    conn.execute(
                        """
                        INSERT INTO positions (title, skills)
                        VALUES (%s, %s)
                        ON CONFLICT (title) DO NOTHING
                        """,
                        (title, json.dumps(skills))
                    )

            return

        except psycopg.OperationalError:
            time.sleep(2)

    raise RuntimeError("Database not ready")


# ============================================================
# Application Lifespan
# ============================================================

@asynccontextmanager
async def lifespan(app):
    initialize()
    yield


# ============================================================
# FastAPI Application
# ============================================================

app = FastAPI(
    title="Profile Matching Demo",
    lifespan=lifespan
)

# Prometheus metrics endpoint
app.mount("/metrics", make_asgi_app())


# ============================================================
# Pydantic Models
# ============================================================
class Resume(BaseModel):
    name: str = Field(min_length=1)
    skills: list[str] = Field(min_length=1)


# ============================================================
# Health Check API
# ============================================================
@app.get("/health")
def health():

    # Check PostgreSQL connection
    with connect() as conn:
        conn.execute("SELECT 1").fetchone()

    # Check Redis connection
    Redis.from_url(QUEUE).ping()

    return {
        "status": "ok"
    }


# ============================================================
# Submit Resume API
# ============================================================

@app.post("/submitResume", status_code=202)
def submit_resume(resume: Resume):

    REQ.labels("submitResume").inc()

    # Generate unique IDs
    profile_id = str(uuid.uuid4())
    job_id = str(uuid.uuid4())

    # Normalize candidate skills
    skills = sorted(
        set(
            skill.strip().lower()
            for skill in resume.skills
            if skill.strip()
        )
    )

    if not skills:
        raise HTTPException(
            status_code=422,
            detail="At least one nonempty skill required"
        )

    # Store candidate profile and processing job
    with connect() as conn:

        conn.execute(
            """
            INSERT INTO profiles (id, name, skills, status)
            VALUES (%s, %s, %s, %s)
            """,
            (
                profile_id,
                resume.name,
                json.dumps(skills),
                "queued"
            )
        )

        conn.execute(
            """
            INSERT INTO jobs (id, profile_id, status)
            VALUES (%s, %s, %s)
            """,
            (
                job_id,
                profile_id,
                "queued"
            )
        )

    # Add processing job to Redis queue
    try:
        Redis.from_url(QUEUE).lpush(
            "resume_jobs",
            json.dumps({
                "job_id": job_id,
                "profile_id": profile_id
            })
        )

    except Exception:
        log.exception(
            "Failed to enqueue job %s",
            job_id
        )

        with connect() as conn:

            conn.execute(
                """
                UPDATE jobs
                SET status = %s
                WHERE id = %s
                """,
                ("enqueue_failed", job_id)
            )

            conn.execute(
                """
                UPDATE profiles
                SET status = %s
                WHERE id = %s
                """,
                ("enqueue_failed", profile_id)
            )

        raise HTTPException(
            status_code=503,
            detail="Queue unavailable"
        )

    log.info(
        "Queued job=%s profile=%s",
        job_id,
        profile_id
    )

    return {
        "job_id": job_id,
        "profile_id": profile_id,
        "status": "queued"
    }


# ============================================================
# Get Candidate Profile API
# ============================================================

@app.get("/profile/{profile_id}")
def profile(profile_id: str):

    REQ.labels("profile").inc()

    with connect() as conn:
        row = conn.execute(
            """
            SELECT id, name, skills, status
            FROM profiles
            WHERE id = %s
            """,
            (profile_id,)
        ).fetchone()

    if not row:
        raise HTTPException(
            status_code=404,
            detail="Profile not found"
        )

    return dict(
        zip(
            ("id", "name", "skills", "status"),
            row
        )
    )


# ============================================================
# Top 3 Job Matching API
# ============================================================

@app.get("/top3Matching/{profile_id}")
def top3_matching(profile_id: str):

    REQ.labels("top3Matching").inc()

    # Fetch candidate skills and processing status
    with connect() as conn:

        row = conn.execute(
            """
            SELECT skills, status
            FROM profiles
            WHERE id = %s
            """,
            (profile_id,)
        ).fetchone()

        if not row:
            raise HTTPException(
                status_code=404,
                detail="Profile not found"
            )

        if row[1] != "processed":
            raise HTTPException(
                status_code=409,
                detail=f"Profile not ready: {row[1]}"
            )

        # Fetch all available job positions
        positions = conn.execute(
            """
            SELECT title, skills
            FROM positions
            """
        ).fetchall()

    # Convert candidate skills to a set
    candidate_skills = set(row[0])

    results = []

    # Calculate matching score for each position
    for title, required_skills in positions:

        required_skills = set(required_skills)

        # Find common skills
        matched_skills = candidate_skills & required_skills

        # Calculate percentage match
        score = round(
            100 * len(matched_skills) / len(required_skills),
            1
        )

        results.append({
            "title": title,
            "score": score,
            "matched_skills": sorted(matched_skills)
        })

    # Sort by highest score, then alphabetically by title
    sorted_results = sorted(
        results,
        key=lambda result: (
            -result["score"],
            result["title"]
        )
    )

    # Return only the top 3 matching positions
    return {
        "profile_id": profile_id,
        "matches": sorted_results[:3]
    }