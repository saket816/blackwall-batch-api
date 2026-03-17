"""
Batch API - Request/Response Models
"""

from pydantic import BaseModel, Field, field_validator
from typing import Optional, List, Dict, Any


# ─────────────────────────────────────────────
# INPUT MODELS
# ─────────────────────────────────────────────

class CoordinateInput(BaseModel):
    """Single coordinate entry from user"""
    latitude: float = Field(..., ge=-90, le=90)
    longitude: float = Field(..., ge=-180, le=180)
    radius: float = Field(..., ge=0.5, le=10.0)
    name: str = Field(..., min_length=1, max_length=200)

    @field_validator('name')
    @classmethod
    def clean_name(cls, v):
        return v.strip()

    class Config:
        json_schema_extra = {
            "example": {
                "latitude": 12.9716,
                "longitude": 77.5946,
                "radius": 2.0,
                "name": "MG Road Bangalore"
            }
        }


class BatchRequest(BaseModel):
    """Full batch submission from user"""
    coordinates: List[CoordinateInput] = Field(
        ...,
        min_length=1,
        description="List of coordinates to analyze"
    )
    batch_name: Optional[str] = Field(
        None,
        max_length=200,
        description="Optional label for this batch e.g. 'Bangalore Run 1'"
    )

    class Config:
        json_schema_extra = {
            "example": {
                "batch_name": "Bangalore Analysis",
                "coordinates": [
                    {"latitude": 12.9716, "longitude": 77.5946, "radius": 2.0, "name": "MG Road"},
                    {"latitude": 12.9352, "longitude": 77.6245, "radius": 2.0, "name": "Koramangala"}
                ]
            }
        }


# ─────────────────────────────────────────────
# JOB-LEVEL MODELS
# ─────────────────────────────────────────────

class JobStatus(BaseModel):
    """Status of a single coordinate job within a batch"""
    name: str
    latitude: float
    longitude: float
    radius: float
    status: str                          # queued / running / completed / failed
    job_id: Optional[str] = None         # job_id from Blackwall API
    blackwall_job_id: Optional[str] = None
    error: Optional[str] = None
    score: Optional[float] = None        # filled when completed
    classification: Optional[str] = None # filled when completed


# ─────────────────────────────────────────────
# BATCH-LEVEL RESPONSE MODELS
# ─────────────────────────────────────────────

class BatchCreateResponse(BaseModel):
    """Returned immediately when batch is submitted"""
    batch_id: str
    batch_name: Optional[str]
    total_coordinates: int
    message: str
    status_url: str
    summary_url: str


class BatchStatusResponse(BaseModel):
    """Live progress of the batch"""
    batch_id: str
    batch_name: Optional[str]
    overall_status: str                  # running / completed / partial_failed
    total: int
    completed: int
    failed: int
    pending: int
    progress_percent: float
    created_at: str
    completed_at: Optional[str]
    jobs: List[JobStatus]                # per-coordinate breakdown


class SummaryReport(BaseModel):
    """Combined summary of all completed analyses"""
    batch_id: str
    batch_name: Optional[str]
    generated_at: str
    total_coordinates: int
    completed: int
    failed: int
    results: List[Dict[str, Any]]        # one entry per coord