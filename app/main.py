import time
from datetime import datetime, timezone
from contextlib import asynccontextmanager
from typing import Optional
from fastapi import FastAPI, HTTPException, status, Query

from app.database import persistence_driver
from app.models import ClockEventPayload, ManualAdjustmentPayload
from app.engine import truncate_to_seconds, calculate_attendance_metrics, IST


@asynccontextmanager
async def service_lifecycle_handler(app: FastAPI):
    """Safely manages database connection lifecycles and creates indexes."""
    persistence_driver.initialize_node()

    if persistence_driver.db is not None:
        # Enforce uniqueness via indexes to guarantee data integrity
        await persistence_driver.db["employees"].create_index(
            "emp_code", unique=True
        )
        await persistence_driver.db["attendance_logs"].create_index(
            [("emp_code", 1), ("date", 1)], unique=True
        )

    yield
    persistence_driver.close_node()


app = FastAPI(
    title="Employee Attendance & Analytics Core",
    version="2.0.0",
    lifespan=service_lifecycle_handler
)


# -------------------------------------------------------------------------
# 1. READINESS PROBE / HEALTH CHECK
# -------------------------------------------------------------------------
@app.get("/health", tags=["system"])
async def execution_readiness_probe():
    """Returns 200 if MongoDB answers a ping, otherwise 503."""
    try:
        if persistence_driver.db is None:
            raise Exception("Database proxy driver unit uninitialized.")

        await persistence_driver.db.command("ping")
        return {"status": "ok"}

    except Exception:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database state structural failure or unreachable."
        )


# -------------------------------------------------------------------------
# 2. BULLETPROOF ATTENDANCE PUNCH ENGINE
# -------------------------------------------------------------------------
@app.post(
    "/api/v1/attendance/punch",
    status_code=status.HTTP_201_CREATED,
    tags=["attendance"]
)
async def execute_attendance_punch(payload: ClockEventPayload):
    """Logs punch actions while enforcing strict sequencing rules."""
    db = persistence_driver.db

    if db is None:
        raise HTTPException(
            status_code=500,
            detail="Database instance unavailable."
        )

    # Locate employee metadata records
    employee = await db["employees"].find_one(
        {"emp_code": payload.employee_id}
    )

    if not employee:
        raise HTTPException(
            status_code=404,
            detail=f"Employee profile {payload.employee_id} not registered."
        )

    # Convert milliseconds safely into UTC datetime objects
    raw_dt = datetime.fromtimestamp(
        payload.epoch_ms / 1000.0,
        tz=timezone.utc
    )
    punch_time = truncate_to_seconds(raw_dt)

    # R1: Calculate target calendar day in IST bounds
    ist_punch = punch_time.astimezone(IST)
    attendance_date = ist_punch.strftime("%Y-%m-%d")

    # Fetch active records for today's slot
    existing_log = await db["attendance_logs"].find_one({
        "emp_code": payload.employee_id,
        "date": attendance_date
    })

    if not existing_log:
        # PUNCH-IN Flow: Compute initial metrics
        late_min, _, _, _ = calculate_attendance_metrics(
            punch_time, None, employee["shift_start"], employee["shift_end"]
        )

        new_attendance_record = {
            "emp_code": payload.employee_id,
            "date": attendance_date,
            "status": payload.action_type,
            "punch_in": punch_time,
            "punch_out": None,
            "work_hours": None,
            "late_minutes": late_min,
            "overtime_minutes": 0,
            "half_day": False,
            "history": []
        }

        await db["attendance_logs"].insert_one(new_attendance_record)

        return {
            "status": "success",
            "message": "Clock-in transaction registered successfully."
        }

    else:
        # PUNCH-OUT Flow: Update metrics safely
        if existing_log["punch_out"] is not None:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Consecutive tracking sequence error. Daily logging "
                    "limits achieved for target node."
                )
            )

        late_min, hours, overtime, half_day = calculate_attendance_metrics(
            existing_log["punch_in"],
            punch_time,
            employee["shift_start"],
            employee["shift_end"]
        )

        await db["attendance_logs"].update_one(
            {"_id": existing_log["_id"]},
            {
                "$set": {
                    "punch_out": punch_time,
                    "work_hours": hours,
                    "late_minutes": late_min,
                    "overtime_minutes": overtime,
                    "half_day": half_day
                }
            }
        )

        return {
            "status": "success",
            "message": "Clock-out shift lifecycle terminated successfully."
        }


# -------------------------------------------------------------------------
# 3. MANUAL CORRECTION & AUDIT ENGINE
# -------------------------------------------------------------------------
@app.post(
    "/api/v1/attendance/correct",
    status_code=status.HTTP_200_OK,
    tags=["admin"]
)
async def execute_manual_data_correction(payload: ManualAdjustmentPayload):
    """Applies admin corrections and creates a secure field-level history trail."""
    db = persistence_driver.db

    if db is None:
        raise HTTPException(
            status_code=500,
            detail="Database instance unavailable."
        )

    # 1. Look up target employee metadata
    employee = await db["employees"].find_one(
        {"emp_code": payload.employee_id}
    )

    if not employee:
        raise HTTPException(
            status_code=404,
            detail=f"Employee profile {payload.employee_id} not registered."
        )

    # 2. Locate active target date attendance entry
    current_record = await db["attendance_logs"].find_one({
        "emp_code": payload.employee_id,
        "date": payload.calendar_date
    })

    if not current_record:
        raise HTTPException(
            status_code=404,
            detail=(
                f"No existing attendance logs found for date "
                f"{payload.calendar_date}."
            )
        )

    # 3. Parse and translate new millisecond targets safely to whole-second UTC datetimes
    revised_in = None

    if payload.forced_in is not None:
        revised_in = truncate_to_seconds(
            datetime.fromtimestamp(
                payload.forced_in / 1000.0,
                tz=timezone.utc
            )
        )

    revised_out = None

    if payload.forced_out is not None:
        revised_out = truncate_to_seconds(
            datetime.fromtimestamp(
                payload.forced_out / 1000.0,
                tz=timezone.utc
            )
        )

    # 4. Compute updated derived metrics using our business calculation layer
    calc_in = revised_in if revised_in else current_record.get("punch_in")
    calc_out = revised_out if revised_out else current_record.get("punch_out")

    late_min, hours, overtime, half_day = calculate_attendance_metrics(
        calc_in,
        calc_out,
        employee["shift_start"],
        employee["shift_end"]
    )

    # Map out target structural state updates
    projected_state = {
        "punch_in": revised_in,
        "punch_out": revised_out,
        "work_hours": hours,
        "late_minutes": late_min,
        "overtime_minutes": overtime,
        "half_day": half_day
    }

    # 5. Delta Analysis Engine: Identify fields that *actually* changed
    state_mutations = {}

    for data_key, new_value in projected_state.items():
        old_value = current_record.get(data_key)

        if old_value != new_value:
            state_mutations[data_key] = {
                "from": (
                    int(old_value.timestamp() * 1000)
                    if isinstance(old_value, datetime)
                    else old_value
                ),
                "to": (
                    int(new_value.timestamp() * 1000)
                    if isinstance(new_value, datetime)
                    else new_value
                )
            }

    # If nothing changed, save database computational cycles and exit early
    if not state_mutations:
        return {
            "status": "unchanged",
            "message": "Supplied state targets match current snapshots."
        }

    # 6. Forge the definitive Audit Log payload element
    historical_audit_node = {
        "at": truncate_to_seconds(datetime.now(timezone.utc)),
        "by": payload.authorized_agent,
        "reason": payload.update_reason,
        "changes": state_mutations
    }

    # 7. Commit changes to MongoDB cleanly using transactional push updates
    await db["attendance_logs"].update_one(
        {"_id": current_record["_id"]},
        {
            "$set": projected_state,
            "$push": {"history": historical_audit_node}
        }
    )

    return {
        "status": "success",
        "message": "Manual adjustment completed and historical audit log appended."
    }


# -------------------------------------------------------------------------
# 4. HIGH-PERFORMANCE AGGREGATION ANALYTICS ENGINE
# -------------------------------------------------------------------------
@app.get("/api/v1/analytics/summary", tags=["analytics"])
async def fetch_organizational_analytics(
    month: str = Query(
        ...,
        description="Target calculation month format YYYY-MM"
    ),
    department: Optional[str] = Query(
        None,
        description="Optional case-sensitive filter"
    )
):
    """Computes high-speed organizational metrics directly within MongoDB layers."""
    db = persistence_driver.db

    if db is None:
        raise HTTPException(
            status_code=500,
            detail="Database instance unavailable."
        )

    # Step A: Filter down attendance logs by target calendar month string sequence
    match_stage = {
        "date": {"$regex": f"^{month}"}
    }

    # Step B: Build relational lookup connection to integrate employee data
    pipeline = [
        {"$match": match_stage},
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "employee_profile"
            }
        },
        {"$unwind": "$employee_profile"}
    ]

    # Step C: Inject case-sensitive department filters dynamically if provided
    if department:
        pipeline.append({
            "$match": {
                "employee_profile.department": department
            }
        })

    # Step D: Execute group-level accumulator mutations to aggregate final metrics
    pipeline.append({
        "$group": {
            "_id": "$emp_code",
            "employee_name": {
                "$first": "$employee_profile.name"
            },
            "total_logged_days": {
                "$sum": 1
            },
            "cumulative_hours": {
                "$sum": {
                    "$ifNull": ["$work_hours", 0]
                }
            },
            "accumulated_late_minutes": {
                "$sum": {
                    "$ifNull": ["$late_minutes", 0]
                }
            },
            "accumulated_overtime_minutes": {
                "$sum": {
                    "$ifNull": ["$overtime_minutes", 0]
                }
            },
            "half_day_occurrences": {
                "$sum": {
                    "$cond": [
                        {"$eq": ["$half_day", True]},
                        1,
                        0
                    ]
                }
            }
        }
    })

    # Step E: Reshape output to conform cleanly with OpenAPI formatting structures
    pipeline.append({
        "$project": {
            "_id": 0,
            "emp_code": "$_id",
            "name": "$employee_name",
            "total_days": "$total_logged_days",
            "total_work_hours": {
                "$round": ["$cumulative_hours", 2]
            },
            "total_late_minutes": "$accumulated_late_minutes",
            "total_overtime_minutes": "$accumulated_overtime_minutes",
            "total_half_days": "$half_day_occurrences"
        }
    })

    try:
        aggregation_cursor = db["attendance_logs"].aggregate(pipeline)
        computed_summary_report = await aggregation_cursor.to_list(length=500)

        return {
            "period": month,
            "department_scope": department if department else "ALL_DEPARTMENTS",
            "metrics": computed_summary_report
        }

    except Exception as stream_err:
        raise HTTPException(
            status_code=500,
            detail=f"Aggregation transaction failed. Context: {str(stream_err)}"
        )