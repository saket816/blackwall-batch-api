"""
Blackwall Batch API
FastAPI server - 4 endpoints for batch coordinate analysis
"""

import asyncio
import traceback
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, HTTPException, BackgroundTasks, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from models import (
    BatchRequest,
    BatchCreateResponse,
    BatchStatusResponse,
    SummaryReport
)
from batch_manager import (
    create_batch,
    get_batch,
    get_batch_status_response,
    get_individual_report,
    get_summary_report,
    process_batch
)

# ─────────────────────────────────────────────
# APP INIT
# ─────────────────────────────────────────────

app = FastAPI(
    title="Blackwall Batch API",
    description="Submit multiple coordinates at once and download individual + combined reports",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─────────────────────────────────────────────
# ROOT
# ─────────────────────────────────────────────

@app.get("/")
async def root():
    """Health check + API info"""
    return {
        "name": "Blackwall Batch API",
        "version": "1.0.0",
        "status": "operational",
        "endpoints": {
            "submit_batch":      "POST /api/v1/batch/analyze",
            "batch_status":      "GET  /api/v1/batch/status/{batch_id}",
            "individual_report": "GET  /api/v1/batch/{batch_id}/report/{coord_name}",
            "summary_report":    "GET  /api/v1/batch/{batch_id}/summary"
        },
        "docs": "/docs"
    }


# ─────────────────────────────────────────────
# ENDPOINT 1 — Submit Batch
# ─────────────────────────────────────────────

@app.post("/api/v1/batch/analyze", response_model=BatchCreateResponse, status_code=202)
async def submit_batch(request: BatchRequest, background_tasks: BackgroundTasks):
    """
    Submit a batch of coordinates for analysis.

    Paste a JSON body like:
    {
        "batch_name": "Bangalore Run 1",
        "coordinates": [
            {"latitude": 12.97, "longitude": 77.59, "radius": 2.0, "name": "MG Road"},
            {"latitude": 12.93, "longitude": 77.62, "radius": 2.0, "name": "Koramangala"}
        ]
    }

    Returns batch_id immediately. Processing runs in background.
    """
    try:
        # Create batch in memory
        batch_id = create_batch(
            coordinates=request.coordinates,
            batch_name=request.batch_name
        )

        # Kick off background processing
        background_tasks.add_task(process_batch, batch_id)

        total = len(request.coordinates)
        estimated_minutes = round(total * 2.5)

        return {
            "batch_id": batch_id,
            "batch_name": request.batch_name or f"Batch {batch_id[:8]}",
            "total_coordinates": total,
            "message": (
                f"Batch created with {total} coordinates. "
                f"Estimated time: ~{estimated_minutes} minutes. "
                f"Poll status_url to track progress."
            ),
            "status_url": f"/api/v1/batch/status/{batch_id}",
            "summary_url": f"/api/v1/batch/{batch_id}/summary"
        }

    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Failed to create batch: {str(e)}")


# ─────────────────────────────────────────────
# ENDPOINT 2 — Batch Status (live progress)
# ─────────────────────────────────────────────

@app.get("/api/v1/batch/status/{batch_id}", response_model=BatchStatusResponse)
async def batch_status(batch_id: str):
    """
    Get live progress of a batch.

    Shows:
    - How many completed / failed / pending
    - Progress percent  e.g. 12/50 done = 24%
    - Per-coordinate status with score when done

    Poll this every 30 seconds to track progress.
    """
    status = get_batch_status_response(batch_id)

    if status is None:
        raise HTTPException(status_code=404, detail=f"Batch not found: {batch_id}")

    return status


# ─────────────────────────────────────────────
# ENDPOINT 3 — Individual Report
# ─────────────────────────────────────────────

@app.get("/api/v1/batch/{batch_id}/report/{coord_name}")
async def individual_report(batch_id: str, coord_name: str):
    """
    Download full report for one coordinate.

    coord_name must match the name you submitted
    (case-insensitive, spaces allowed).

    Example:
        /api/v1/batch/abc123/report/MG Road
        /api/v1/batch/abc123/report/mg_road

    Returns full JSON report including score breakdown,
    brand presence, investment insights.

    Only available once that coordinate's status is 'completed'.
    """
    # Check batch exists
    batch = get_batch(batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail=f"Batch not found: {batch_id}")

    # Get job
    job = get_individual_report(batch_id, coord_name)
    if job is None:
        raise HTTPException(
            status_code=404,
            detail=f"Coordinate '{coord_name}' not found in batch. Check the name and try again."
        )

    # Not completed yet
    if job["status"] == "queued":
        raise HTTPException(
            status_code=400,
            detail=f"'{coord_name}' is still queued. Check /status for progress."
        )

    if job["status"] == "running":
        raise HTTPException(
            status_code=400,
            detail=f"'{coord_name}' is currently being analyzed. Check back in a few minutes."
        )

    if job["status"] == "failed":
        raise HTTPException(
            status_code=422,
            detail={
                "message": f"Analysis failed for '{coord_name}'.",
                "error": job.get("error", "Unknown error")
            }
        )

    # Return full report

    safe_filename = job["name"].strip().replace(" ", "_").replace(",", "").replace(".", "") + ".json"

    return JSONResponse(
        content={
            "coord_name": job["name"],
            "latitude": job["latitude"],
            "longitude": job["longitude"],
            "radius": job["radius"],
            "score": job["score"],
            "classification": job["classification"],
            "completed_at": job["completed_at"],
            "full_report": job["result"]
        },
        headers={
            "Content-Disposition": f"attachment; filename={safe_filename}"
        }
    )


# ─────────────────────────────────────────────
# ENDPOINT 4 — Combined Summary Report
# ─────────────────────────────────────────────

@app.get("/api/v1/batch/{batch_id}/summary")
async def summary_report(batch_id: str):
    """
    Download combined summary of all coordinates in the batch.

    Returns one JSON file with:
    - Score + grade for every coordinate
    - Score breakdown (roads / commercial / amenities)
    - Investment insights per location
    - Failed coordinates with error reason

    Available at any time — shows results for completed ones,
    pending status for others. Best to fetch after batch is 100% done.
    """
    batch = get_batch(batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail=f"Batch not found: {batch_id}")

    summary = get_summary_report(batch_id)
    if summary is None:
        raise HTTPException(status_code=500, detail="Failed to generate summary.")

    return JSONResponse(content=summary)


# ─────────────────────────────────────────────
# ERROR HANDLERS
# ─────────────────────────────────────────────

@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    traceback.print_exc()
    return JSONResponse(
        status_code=500,
        content={
            "detail": "Internal server error.",
            "error_type": type(exc).__name__
        }
    )


# ─────────────────────────────────────────────
# RUN
# ─────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001)

