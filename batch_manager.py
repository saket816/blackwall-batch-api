"""
Batch Manager
Handles all batch logic - creating, tracking, polling Blackwall API
"""

import uuid
import asyncio
import httpx
from datetime import datetime, timezone
from typing import Dict, Optional

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────

import os
from dotenv import load_dotenv

load_dotenv()

BLACKWALL_BASE_URL = os.getenv("BLACKWALL_BASE_URL", "https://blackwall-hotspot-v2.onrender.com/api/v1")

# How often to poll each job's status (seconds)
POLL_INTERVAL = 15

# Max time to wait for a single job before marking it failed (seconds)
JOB_TIMEOUT = 600  # 10 minutes


# ─────────────────────────────────────────────
# IN-MEMORY STORE
# ─────────────────────────────────────────────

# All batches live here
# Structure:
# {
#   "batch_id": {
#       "batch_id": str,
#       "batch_name": str,
#       "created_at": str,
#       "completed_at": str | None,
#       "overall_status": str,
#       "jobs": {
#           "coord_name": {
#               "name": str,
#               "latitude": float,
#               "longitude": float,
#               "radius": float,
#               "status": queued/running/completed/failed,
#               "blackwall_job_id": str | None,
#               "result": dict | None,
#               "error": str | None,
#               "score": float | None,
#               "classification": str | None,
#               "submitted_at": str | None,
#               "completed_at": str | None,
#           }
#       }
#   }
# }

batches: Dict[str, Dict] = {}


# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def make_safe_key(name: str) -> str:
    """
    Convert coord name to a safe dict key
    e.g. 'MG Road Bangalore' -> 'mg_road_bangalore'
    """
    return name.strip().lower().replace(" ", "_").replace(",", "").replace(".", "")


def get_batch_stats(batch: Dict) -> Dict:
    """Count completed / failed / pending jobs in a batch"""
    jobs = batch["jobs"]
    total = len(jobs)
    completed = sum(1 for j in jobs.values() if j["status"] == "completed")
    failed = sum(1 for j in jobs.values() if j["status"] == "failed")
    pending = total - completed - failed
    progress = round((completed + failed) / total * 100, 1) if total > 0 else 0.0

    return {
        "total": total,
        "completed": completed,
        "failed": failed,
        "pending": pending,
        "progress_percent": progress
    }


# ─────────────────────────────────────────────
# BLACKWALL API CALLS
# ─────────────────────────────────────────────

async def submit_to_blackwall(client: httpx.AsyncClient, coord: Dict) -> str:
    """
    Submit one coordinate to Blackwall API
    Returns blackwall job_id
    """
    payload = {
        "latitude": coord["latitude"],
        "longitude": coord["longitude"],
        "radius": coord["radius"],
        "location_name": coord["name"]
    }

    response = await client.post(
        f"{BLACKWALL_BASE_URL}/analyze",
        json=payload,
        timeout=30
    )
    response.raise_for_status()
    data = response.json()
    return data["job_id"]


async def poll_blackwall_status(client: httpx.AsyncClient, blackwall_job_id: str) -> Dict:
    """
    Poll status of one job from Blackwall API
    Returns status dict
    """
    response = await client.get(
        f"{BLACKWALL_BASE_URL}/status/{blackwall_job_id}",
        timeout=30
    )
    response.raise_for_status()
    return response.json()


async def fetch_blackwall_report(client: httpx.AsyncClient, blackwall_job_id: str) -> Dict:
    """
    Fetch final report from Blackwall API once job is completed
    Returns full report dict
    """
    response = await client.get(
        f"{BLACKWALL_BASE_URL}/report/{blackwall_job_id}",
        timeout=30
    )
    response.raise_for_status()
    return response.json()


# ─────────────────────────────────────────────
# CORE BATCH PROCESSOR
# ─────────────────────────────────────────────

async def process_single_coord(
    client: httpx.AsyncClient,
    batch_id: str,
    coord_key: str
):
    """
    Full lifecycle for one coordinate:
    1. Submit to Blackwall
    2. Poll until done
    3. Fetch report
    4. Store result
    """
    job = batches[batch_id]["jobs"][coord_key]

    try:
        # ── Step 1: Submit ──
        job["status"] = "running"
        job["submitted_at"] = now_iso()

        blackwall_job_id = await submit_to_blackwall(client, job)
        job["blackwall_job_id"] = blackwall_job_id

        print(f"  ▶ Submitted [{job['name']}] → blackwall job: {blackwall_job_id}")

        # ── Step 2: Poll until completed or failed ──
        elapsed = 0

        while elapsed < JOB_TIMEOUT:
            await asyncio.sleep(POLL_INTERVAL)
            elapsed += POLL_INTERVAL

            status_data = await poll_blackwall_status(client, blackwall_job_id)
            bw_status = status_data.get("status", "")

            print(f"  ⏳ [{job['name']}] status: {bw_status} ({elapsed}s elapsed)")

            if bw_status == "completed":
                break

            if bw_status == "failed":
                error_msg = status_data.get("error", "Blackwall reported failure")
                raise Exception(f"Blackwall job failed: {error_msg}")

        else:
            # Timeout
            raise Exception(f"Job timed out after {JOB_TIMEOUT} seconds")

        # ── Step 3: Fetch Report ──
        report = await fetch_blackwall_report(client, blackwall_job_id)

        # ── Step 4: Store Result ──
        job["status"] = "completed"
        job["completed_at"] = now_iso()
        job["result"] = report

        # Extract key fields for quick access
        analysis = report.get("analysis", {})
        final_score = analysis.get("final_score", {})
        job["score"] = final_score.get("total_points")
        job["classification"] = final_score.get("classification")

        print(f"  ✅ [{job['name']}] done — Score: {job['score']}/100 ({job['classification']})")

    except Exception as e:
        job["status"] = "failed"
        job["completed_at"] = now_iso()
        job["error"] = str(e)
        print(f"  ❌ [{job['name']}] failed: {str(e)}")


async def process_batch(batch_id: str):
    """
    Process all coordinates in a batch sequentially
    (Blackwall API has a single worker queue so sequential is correct)
    """
    batch = batches[batch_id]
    print(f"\n🚀 Starting batch [{batch_id}] — {len(batch['jobs'])} coordinates")

    async with httpx.AsyncClient() as client:
        for coord_key, job in batch["jobs"].items():
            if batch_id not in batches:
                # Batch was deleted mid-run
                return
            await process_single_coord(client, batch_id, coord_key)

    # ── Mark batch complete ──
    stats = get_batch_stats(batch)
    batch["completed_at"] = now_iso()

    if stats["failed"] == 0:
        batch["overall_status"] = "completed"
    elif stats["completed"] == 0:
        batch["overall_status"] = "failed"
    else:
        batch["overall_status"] = "partial_failed"

    print(f"\n✅ Batch [{batch_id}] finished — {stats['completed']}/{stats['total']} succeeded")


# ─────────────────────────────────────────────
# PUBLIC FUNCTIONS (called by main.py)
# ─────────────────────────────────────────────

def create_batch(coordinates: list, batch_name: Optional[str] = None) -> str:
    """
    Create a new batch from list of coordinate dicts
    Returns batch_id
    """
    batch_id = str(uuid.uuid4())

    jobs = {}
    for coord in coordinates:
        # Handle both dict and pydantic model
        if hasattr(coord, "model_dump"):
            coord = coord.model_dump()

        key = make_safe_key(coord["name"])

        # If duplicate names exist, append index
        original_key = key
        counter = 1
        while key in jobs:
            key = f"{original_key}_{counter}"
            counter += 1

        jobs[key] = {
            "name": coord["name"],
            "latitude": coord["latitude"],
            "longitude": coord["longitude"],
            "radius": coord["radius"],
            "status": "queued",
            "blackwall_job_id": None,
            "result": None,
            "error": None,
            "score": None,
            "classification": None,
            "submitted_at": None,
            "completed_at": None,
        }

    batches[batch_id] = {
        "batch_id": batch_id,
        "batch_name": batch_name or f"Batch {batch_id[:8]}",
        "created_at": now_iso(),
        "completed_at": None,
        "overall_status": "running",
        "jobs": jobs
    }

    print(f"📝 Created batch [{batch_id}] with {len(jobs)} coordinates")
    return batch_id


def get_batch(batch_id: str) -> Optional[Dict]:
    """Get full batch data or None if not found"""
    return batches.get(batch_id)


def get_batch_status_response(batch_id: str) -> Optional[Dict]:
    """
    Build status response dict for an endpoint
    """
    batch = batches.get(batch_id)
    if not batch:
        return None

    stats = get_batch_stats(batch)

    jobs_list = []
    for coord_key, job in batch["jobs"].items():
        jobs_list.append({
            "name": job["name"],
            "latitude": job["latitude"],
            "longitude": job["longitude"],
            "radius": job["radius"],
            "status": job["status"],
            "blackwall_job_id": job["blackwall_job_id"],
            "error": job.get("error"),
            "score": job.get("score"),
            "classification": job.get("classification")
        })

    return {
        "batch_id": batch_id,
        "batch_name": batch["batch_name"],
        "overall_status": batch["overall_status"],
        "total": stats["total"],
        "completed": stats["completed"],
        "failed": stats["failed"],
        "pending": stats["pending"],
        "progress_percent": stats["progress_percent"],
        "created_at": batch["created_at"],
        "completed_at": batch.get("completed_at"),
        "jobs": jobs_list
    }


def get_individual_report(batch_id: str, coord_name: str) -> Optional[Dict]:
    """
    Get report for one coordinate by name
    Returns None if not found or not completed
    """
    batch = batches.get(batch_id)
    if not batch:
        return None

    key = make_safe_key(coord_name)
    job = batch["jobs"].get(key)
    if not job:
        return None

    return job  # caller checks status


def get_summary_report(batch_id: str) -> Optional[Dict]:
    """
    Build combined summary of all completed jobs
    """
    batch = batches.get(batch_id)
    if not batch:
        return None

    stats = get_batch_stats(batch)
    results = []

    for coord_key, job in batch["jobs"].items():
        entry = {
            "name": job["name"],
            "latitude": job["latitude"],
            "longitude": job["longitude"],
            "radius": job["radius"],
            "status": job["status"],
            "score": job.get("score"),
            "classification": job.get("classification"),
            "error": job.get("error")
        }

        # Add score breakdown if available
        if job.get("result"):
            analysis = job["result"].get("analysis", {})
            breakdown = analysis.get("score_breakdown", {})
            entry["score_breakdown"] = {
                "roads": breakdown.get("roads", {}).get("score"),
                "commercial": breakdown.get("commercial", {}).get("score"),
                "amenities": breakdown.get("amenities", {}).get("score")
            }
            entry["investment_insights"] = analysis.get("investment_insights", {})

        results.append(entry)

    return {
        "batch_id": batch_id,
        "batch_name": batch["batch_name"],
        "generated_at": now_iso(),
        "total_coordinates": stats["total"],
        "completed": stats["completed"],
        "failed": stats["failed"],
        "results": results
    }