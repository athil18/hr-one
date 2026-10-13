"""
Employee Attendance & Analytics API

Starts with: uvicorn app.main:app --port 8000
Environment: MONGO_URI, MONGO_DB (read from environment, .env as fallback)
"""
import os
import math
import calendar
from datetime import datetime, date, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, List, Dict, Any, Union
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Path, status
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from pymongo import MongoClient
import pymongo.errors

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))


def init_db():
    """Create all required indexes idempotently at application startup."""
    # employees collection
    db.employees.create_index([("emp_code", 1)], unique=True)
    db.employees.create_index([("department", 1), ("joined_on", 1)])
    db.employees.create_index([("joined_on", 1)])

    # attendance_logs collection
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
    db.attendance_logs.create_index([("emp_code", 1), ("date", -1)])
    db.attendance_logs.create_index([("status", 1), ("date", -1), ("emp_code", 1)])
    db.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)])
    db.attendance_logs.create_index([("date", 1), ("late_minutes", 1)])


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    """Convert a datetime (naive UTC or tz-aware) to epoch milliseconds integer."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def from_epoch_ms(ms: int) -> datetime:
    """Convert epoch milliseconds integer to whole-second truncated UTC datetime."""
    truncated_ms = (ms // 1000) * 1000
    return datetime.fromtimestamp(truncated_ms / 1000, tz=timezone.utc)


def now_utc_truncated() -> datetime:
    """Return current UTC time truncated to whole seconds."""
    now_ms = (int(datetime.now(timezone.utc).timestamp()) * 1000)
    return datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc)


def to_ist(dt: datetime) -> datetime:
    """Ensure datetime is tz-aware in IST."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(IST)


def get_attendance_date_and_shifts(punch_in_ist: datetime, shift_start: str, shift_end: str):
    """
    R1: Shift times and attendance dates are IST (UTC+05:30).
    A record's date is the IST calendar date of its punch_in instant.
    Overnight shifts (shift_end <= shift_start): a punch-in whose IST time is earlier
    than shift_end belongs to the shift that started the previous day.
    Shift end of an overnight shift falls on the next calendar day.
    """
    s_h, s_m = map(int, shift_start.split(":"))
    e_h, e_m = map(int, shift_end.split(":"))
    is_overnight = (e_h, e_m) <= (s_h, s_m)

    cur_date = punch_in_ist.date()
    if is_overnight and punch_in_ist.time() < time(e_h, e_m):
        attendance_date = cur_date - timedelta(days=1)
    else:
        attendance_date = cur_date

    shift_start_dt = datetime(
        attendance_date.year, attendance_date.month, attendance_date.day,
        s_h, s_m, 0, tzinfo=IST
    )

    if is_overnight:
        shift_end_date = attendance_date + timedelta(days=1)
    else:
        shift_end_date = attendance_date

    shift_end_dt = datetime(
        shift_end_date.year, shift_end_date.month, shift_end_date.day,
        e_h, e_m, 0, tzinfo=IST
    )

    return attendance_date.isoformat(), shift_start_dt, shift_end_dt


def compute_late_minutes(punch_in_ist: datetime, shift_start_dt: datetime) -> int:
    """
    R2: Grace period 10 minutes.
    Late only if punch-in is strictly more than 10:00 after shift_start;
    then late_minutes = whole minutes (floor) since shift_start.
    09:40:00 -> 0; 09:40:01 -> 10; 10:15:59 -> 45.
    """
    diff_sec = int((punch_in_ist - shift_start_dt).total_seconds())
    if diff_sec > 600:
        return diff_sec // 60
    return 0


def compute_work_hours(punch_in_dt: datetime, punch_out_dt: datetime) -> Optional[float]:
    """
    R4: (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up.
    """
    diff_sec = int((punch_out_dt - punch_in_dt).total_seconds())
    hours_dec = (Decimal(diff_sec) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(hours_dec)


def compute_overtime(punch_out_ist: datetime, shift_end_dt: datetime) -> int:
    """
    R3: Whole minutes (floor) between shift_end and punch_out,
    counted only when >= 30; otherwise 0.
    """
    diff_sec = int((punch_out_ist - shift_end_dt).total_seconds())
    mins = diff_sec // 60
    return mins if mins >= 30 else 0


def compute_half_day(work_hours: Optional[float]) -> bool:
    """
    R5: After punch-out, half_day is true when rounded work_hours < 4.50.
    """
    return bool(work_hours is not None and work_hours < 4.50)


def round_half_up(value: Optional[float], decimals: int = 2) -> Optional[float]:
    """R8: 2 decimals half-up (rates: 4 decimals half-up)."""
    if value is None:
        return None
    fmt = "0." + "0" * decimals if decimals > 0 else "1"
    return float(Decimal(str(value)).quantize(Decimal(fmt), rounding=ROUND_HALF_UP))


def serialize_history_entry(entry: Dict[str, Any]) -> Dict[str, Any]:
    changes_ser = {}
    for k, v in entry.get("changes", {}).items():
        from_val = v.get("from")
        to_val = v.get("to")
        if isinstance(from_val, datetime):
            from_val = to_epoch_ms(from_val)
        if isinstance(to_val, datetime):
            to_val = to_epoch_ms(to_val)
        changes_ser[k] = {"from": from_val, "to": to_val}

    return {
        "at": to_epoch_ms(entry["at"]),
        "by": entry["by"],
        "reason": entry["reason"],
        "changes": changes_ser,
    }


def serialize_attendance_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": to_epoch_ms(doc.get("punch_in")),
        "punch_out": to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": bool(doc.get("half_day", False)),
        "history": [serialize_history_entry(h) for h in doc.get("history", [])],
    }


def serialize_employee_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": to_epoch_ms(doc["created_at"]),
    }


def get_month_range(month_str: str):
    """Return month_start ('YYYY-MM-01') and month_end ('YYYY-MM-DD')."""
    year, month = map(int, month_str.split("-"))
    last_day = calendar.monthrange(year, month)[1]
    return f"{month_str}-01", f"{month_str}-{last_day:02d}", last_day


# --------------------------------------------------------------------------- #
# Request Models
# --------------------------------------------------------------------------- #
class EmployeeCreate(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(default=None, ge=100000000000, le=4102444800000)
    status: str = Field(default="PRESENT", pattern=r"^(PRESENT|WFH|ON_DUTY)$")


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(default=None, ge=100000000000, le=4102444800000)


class RegularizeRequest(BaseModel):
    status: Optional[str] = Field(default=None, pattern=r"^(PRESENT|WFH|ON_DUTY|ABSENT|LEAVE)$")
    punch_in: Optional[int] = Field(default=None, ge=100000000000, le=4102444800000)
    punch_out: Optional[int] = Field(default=None, ge=100000000000, le=4102444800000)
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# System & Employee Endpoints
# --------------------------------------------------------------------------- #
@app.get("/", response_class=HTMLResponse, include_in_schema=False)
@app.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
def get_dashboard():
    dashboard_file = os.path.join(os.path.dirname(__file__), "dashboard.html")
    if os.path.exists(dashboard_file):
        with open(dashboard_file, "r", encoding="utf-8") as f:
            return f.read()
    return "<h1>HROne Attendance & Intelligence API</h1><p>Visit <a href='/docs'>/docs</a> for API documentation.</p>"


@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=503, detail="Database unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    if body.shift_start == body.shift_end:
        raise HTTPException(status_code=422, detail="shift_start must differ from shift_end")

    doc = body.model_dump()
    doc["created_at"] = now_utc_truncated()

    try:
        db.employees.insert_one(doc)
    except pymongo.errors.DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")

    return serialize_employee_doc(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    q = {}
    if department:
        q["department"] = department

    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    items_cur = db.employees.find(q).sort("emp_code", 1).skip(skip).limit(page_size)
    items = [serialize_employee_doc(doc) for doc in items_cur]

    return {"items": items, "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Attendance Endpoints
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        ts_utc = from_epoch_ms(body.punched_at)
    else:
        ts_utc = now_utc_truncated()

    ts_ist = to_ist(ts_utc)
    date_str, shift_start_dt, _ = get_attendance_date_and_shifts(
        ts_ist, emp["shift_start"], emp["shift_end"]
    )

    doc = {
        "emp_code": body.emp_code,
        "date": date_str,
        "status": body.status,
        "punch_in": ts_utc,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts_ist, shift_start_dt),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except pymongo.errors.DuplicateKeyError:
        raise HTTPException(status_code=409, detail="already punched in for this date")

    return serialize_attendance_doc(doc)


@app.post("/attendance/punch-out", status_code=200)
def punch_out(body: PunchOutRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        ts_utc = from_epoch_ms(body.punched_at)
    else:
        ts_utc = now_utc_truncated()

    # Find employee's most recent record whose punch_in <= punched_at
    latest_rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts_utc}},
        sort=[("punch_in", -1)]
    )

    if not latest_rec:
        raise HTTPException(status_code=404, detail="No matching punch-in found")

    if latest_rec.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="Record is already punched out")

    punch_in_utc = latest_rec["punch_in"]
    if punch_in_utc.tzinfo is None:
        punch_in_utc = punch_in_utc.replace(tzinfo=timezone.utc)

    # Validation: punched_at <= punch_in or > 24 hours after punch_in
    diff_sec = int((ts_utc - punch_in_utc).total_seconds())
    if diff_sec <= 0:
        raise HTTPException(status_code=422, detail="punch_out must be strictly after punch_in")
    if diff_sec > 24 * 3600:
        raise HTTPException(status_code=422, detail="punch_out cannot be more than 24 hours after punch_in")

    punch_in_ist = to_ist(punch_in_utc)
    punch_out_ist = to_ist(ts_utc)
    _, _, shift_end_dt = get_attendance_date_and_shifts(
        punch_in_ist, emp["shift_start"], emp["shift_end"]
    )

    work_hours = compute_work_hours(punch_in_utc, ts_utc)
    overtime_mins = compute_overtime(punch_out_ist, shift_end_dt)
    half_day = compute_half_day(work_hours)

    # Race-safe update: match punch_out == None
    updated = db.attendance_logs.find_one_and_update(
        {
            "emp_code": body.emp_code,
            "date": latest_rec["date"],
            "punch_out": None
        },
        {
            "$set": {
                "punch_out": ts_utc,
                "work_hours": work_hours,
                "overtime_minutes": overtime_mins,
                "half_day": half_day,
            }
        },
        return_document=pymongo.ReturnDocument.AFTER
    )

    if not updated:
        raise HTTPException(status_code=409, detail="Record was punched out concurrently")

    return serialize_attendance_doc(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status: Optional[str] = Query(default=None, pattern=r"^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from must be <= date_to")

    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.attendance_logs.find(q).sort([("date", -1), ("emp_code", 1)]).skip(skip).limit(page_size)
    items = [serialize_attendance_doc(doc) for doc in cursor]

    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    emp_code: str = Path(...),
    date: str = Path(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
    body: RegularizeRequest = ...,
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not rec:
        raise HTTPException(status_code=404, detail="Attendance record not found for this date")

    # Determine final values
    target_status = body.status if body.status is not None else rec["status"]

    if target_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(status_code=422, detail="Cannot supply punch times when status is ABSENT or LEAVE")
        final_punch_in = None
        final_punch_out = None
    else:
        # Presence status requires punch_in
        if body.punch_in is not None:
            final_punch_in = from_epoch_ms(body.punch_in)
        else:
            final_punch_in = rec.get("punch_in")

        if final_punch_in is None:
            raise HTTPException(status_code=422, detail="Presence status requires a punch_in")

        if body.punch_out is not None:
            final_punch_out = from_epoch_ms(body.punch_out)
        else:
            final_punch_out = rec.get("punch_out")

    # R1: punch_in must stay on record's attendance date
    if final_punch_in is not None:
        p_in_ist = to_ist(final_punch_in)
        calc_date, shift_start_dt, shift_end_dt = get_attendance_date_and_shifts(
            p_in_ist, emp["shift_start"], emp["shift_end"]
        )
        if calc_date != date:
            raise HTTPException(status_code=422, detail=f"punch_in belongs to attendance date {calc_date}, does not match record date {date}")
    else:
        shift_start_dt = None
        shift_end_dt = None

    # Validation: punch_out > punch_in and <= 24 hours
    if final_punch_out is not None:
        if final_punch_in is None:
            raise HTTPException(status_code=422, detail="punch_out cannot exist without punch_in")
        p_in_utc = final_punch_in if final_punch_in.tzinfo else final_punch_in.replace(tzinfo=timezone.utc)
        p_out_utc = final_punch_out if final_punch_out.tzinfo else final_punch_out.replace(tzinfo=timezone.utc)
        diff_sec = int((p_out_utc - p_in_utc).total_seconds())
        if diff_sec <= 0:
            raise HTTPException(status_code=422, detail="punch_out must be after punch_in")
        if diff_sec > 24 * 3600:
            raise HTTPException(status_code=422, detail="punch_out must be within 24 hours of punch_in")

    # Recompute derived fields
    if target_status in ("ABSENT", "LEAVE"):
        new_work_hours = None
        new_late_minutes = 0
        new_overtime_minutes = 0
        new_half_day = False
    else:
        new_late_minutes = compute_late_minutes(to_ist(final_punch_in), shift_start_dt)
        if final_punch_out is not None:
            new_work_hours = compute_work_hours(final_punch_in, final_punch_out)
            new_overtime_minutes = compute_overtime(to_ist(final_punch_out), shift_end_dt)
            new_half_day = compute_half_day(new_work_hours)
        else:
            new_work_hours = None
            new_overtime_minutes = 0
            new_half_day = False

    # Calculate exact changes
    changes = {}

    def check_change(field: str, old_val: Any, new_val: Any):
        # Normalize datetimes for comparison
        cmp_old = to_epoch_ms(old_val) if isinstance(old_val, datetime) else old_val
        cmp_new = to_epoch_ms(new_val) if isinstance(new_val, datetime) else new_val
        if cmp_old != cmp_new:
            changes[field] = {"from": old_val, "to": new_val}

    check_change("status", rec["status"], target_status)
    check_change("punch_in", rec.get("punch_in"), final_punch_in)
    check_change("punch_out", rec.get("punch_out"), final_punch_out)
    check_change("work_hours", rec.get("work_hours"), new_work_hours)
    check_change("late_minutes", rec.get("late_minutes", 0), new_late_minutes)
    check_change("overtime_minutes", rec.get("overtime_minutes", 0), new_overtime_minutes)
    check_change("half_day", bool(rec.get("half_day", False)), new_half_day)

    if not changes:
        raise HTTPException(status_code=422, detail="Request changed nothing")

    history_entry = {
        "at": now_utc_truncated(),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    # Concurrency safe: match current history length
    current_history_len = len(rec.get("history", []))
    res = db.attendance_logs.find_one_and_update(
        {
            "emp_code": emp_code,
            "date": date,
            "history": {"$size": current_history_len}
        },
        {
            "$set": {
                "status": target_status,
                "punch_in": final_punch_in,
                "punch_out": final_punch_out,
                "work_hours": new_work_hours,
                "late_minutes": new_late_minutes,
                "overtime_minutes": new_overtime_minutes,
                "half_day": new_half_day,
            },
            "$push": {
                "history": history_entry
            }
        },
        return_document=pymongo.ReturnDocument.AFTER
    )

    if not res:
        raise HTTPException(status_code=409, detail="Concurrent modification conflict")

    return serialize_attendance_doc(res)


# --------------------------------------------------------------------------- #
# Analytics Endpoints
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str = Path(...),
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    month_start, month_end, last_day = get_month_range(month)

    # Calculate working days (Mon-Fri) from joined_on
    joined_on = emp["joined_on"]
    working_days = 0
    if joined_on <= month_end:
        start_date_str = max(month_start, joined_on)
        curr = date.fromisoformat(start_date_str)
        end = date.fromisoformat(month_end)
        while curr <= end:
            if curr.weekday() < 5:  # Mon-Fri
                working_days += 1
            curr += timedelta(days=1)

    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": month_start, "$lte": month_end}
            }
        },
        {
            "$project": {
                "date": 1,
                "status": 1,
                "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]},
                "half_day": {"$ifNull": ["$half_day", False]},
                "dayOfWeek": {"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date"}}}
            }
        },
        {
            "$group": {
                "_id": None,
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
                "total_late_minutes": {"$sum": "$late_minutes"},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$lte": ["$dayOfWeek", 5]}  # Mon-Fri only
                                ]
                            },
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0
                        ]
                    }
                }
            }
        }
    ]

    agg_res = list(db.attendance_logs.aggregate(pipeline))
    if agg_res:
        stats = agg_res[0]
        present_days = stats["present_days"]
        leave_days = stats["leave_days"]
        late_count = stats["late_count"]
        total_late = stats["total_late_minutes"]
        total_ot = stats["total_overtime_minutes"]
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late = 0
        total_ot = 0

    if working_days > 0:
        attendance_pct = round_half_up((present_days / working_days) * 100, 2)
    else:
        attendance_pct = None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late,
        "total_overtime_minutes": total_ot,
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = Query(default=None),
):
    month_start, month_end, _ = get_month_range(month)

    match_stage = {"joined_on": {"$lte": month_end}}
    if department:
        match_stage["department"] = department

    pipeline = [
        {"$match": match_stage},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"emp": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$emp"]},
                                    {"$gte": ["$date", month_start]},
                                    {"$lte": ["$date", month_end]}
                                ]
                            }
                        }
                    },
                    {
                        "$project": {
                            "status": 1,
                            "work_hours": 1,
                            "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                            "half_day": {"$ifNull": ["$half_day", False]},
                            "dayOfWeek": {"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date"}}}
                        }
                    },
                    {
                        "$group": {
                            "_id": None,
                            "present_days": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                                {"$lte": ["$dayOfWeek", 5]}
                                            ]
                                        },
                                        {"$cond": ["$half_day", 0.5, 1.0]},
                                        0.0
                                    ]
                                }
                            },
                            "work_hours_sum": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                                {"$ne": ["$work_hours", None]}
                                            ]
                                        },
                                        "$work_hours",
                                        0.0
                                    ]
                                }
                            },
                            "work_hours_count": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                                {"$ne": ["$work_hours", None]}
                                            ]
                                        },
                                        1,
                                        0
                                    ]
                                }
                            },
                            "late_count": {
                                "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                            },
                            "total_late_minutes": {"$sum": "$late_minutes"},
                            "leave_count": {
                                "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                            },
                            "on_duty_count": {
                                "$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}
                            }
                        }
                    }
                ],
                "as": "stats"
            }
        },
        {
            "$addFields": {
                "stat": {
                    "$ifNull": [
                        {"$arrayElemAt": ["$stats", 0]},
                        {
                            "present_days": 0.0,
                            "work_hours_sum": 0.0,
                            "work_hours_count": 0,
                            "late_count": 0,
                            "total_late_minutes": 0,
                            "leave_count": 0,
                            "on_duty_count": 0
                        }
                    ]
                }
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present_days": {"$sum": "$stat.present_days"},
                "work_hours_sum": {"$sum": "$stat.work_hours_sum"},
                "work_hours_count": {"$sum": "$stat.work_hours_count"},
                "late_count": {"$sum": "$stat.late_count"},
                "total_late_minutes": {"$sum": "$stat.total_late_minutes"},
                "leave_count": {"$sum": "$stat.leave_count"},
                "on_duty_count": {"$sum": "$stat.on_duty_count"}
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": 1,
                "present_days": 1,
                "work_hours_sum": 1,
                "work_hours_count": 1,
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1
            }
        },
        {"$sort": {"department": 1}}
    ]

    items = []
    for doc in db.employees.aggregate(pipeline):
        count = doc["work_hours_count"]
        total_wh = doc["work_hours_sum"]
        avg_wh = round_half_up(total_wh / count, 2) if count > 0 else None
        items.append({
            "department": doc["department"],
            "headcount": doc["headcount"],
            "present_days": doc["present_days"],
            "avg_work_hours": avg_wh,
            "late_count": doc["late_count"],
            "total_late_minutes": doc["total_late_minutes"],
            "leave_count": doc["leave_count"],
            "on_duty_count": doc["on_duty_count"],
        })

    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(default=10, ge=1, le=50),
    department: Optional[str] = Query(default=None),
):
    month_start, month_end, _ = get_month_range(month)

    pipeline = [
        {
            "$match": {
                "date": {"$gte": month_start, "$lte": month_end},
                "late_minutes": {"$gt": 0}
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1}
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp"
            }
        },
        {"$unwind": "$emp"},
    ]

    if department:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend([
        {
            "$project": {
                "_id": 0,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {"$rank": {}}
                }
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}}
    ])

    items = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str = Path(...),
    from_date: str = Query(..., alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: str = Query(..., alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
):
    # Check department exists
    dept_exists = db.employees.find_one({"department": department})
    if not dept_exists:
        raise HTTPException(status_code=404, detail="Department not found")

    d_from = date.fromisoformat(from_date)
    d_to = date.fromisoformat(to_date)
    if d_to < d_from:
        raise HTTPException(status_code=422, detail="'to' date must be >= 'from' date")
    if (d_to - d_from).days + 1 > 92:
        raise HTTPException(status_code=422, detail="Date range cannot exceed 92 days")

    from_dt = datetime(d_from.year, d_from.month, d_from.day, 0, 0, 0, tzinfo=timezone.utc)
    to_bound_dt = datetime(d_to.year, d_to.month, d_to.day, 0, 0, 0, tzinfo=timezone.utc) + timedelta(days=1)

    pipeline = [
        {"$match": {"department": department}},
        {"$limit": 1},
        {"$project": {"date_dt": from_dt, "dept": department}},
        {
            "$densify": {
                "field": "date_dt",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [from_dt, to_bound_dt]
                }
            }
        },
        {
            "$fill": {
                "sortBy": {"date_dt": 1},
                "output": {"dept": {"method": "locf"}}
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date_dt"}},
                "dayOfWeek": {"$isoDayOfWeek": "$date_dt"}
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"cur_date": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                    {"$lte": ["$joined_on", "$$cur_date"]}
                                ]
                            }
                        }
                    },
                    {"$project": {"_id": 0, "emp_code": 1}}
                ],
                "as": "active_emps"
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"cur_date": "$date", "emps": "$active_emps.emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$date", "$$cur_date"]},
                                    {"$in": ["$emp_code", "$$emps"]}
                                ]
                            }
                        }
                    },
                    {
                        "$project": {
                            "status": 1,
                            "half_day": {"$ifNull": ["$half_day", False]},
                            "late_minutes": {"$ifNull": ["$late_minutes", 0]}
                        }
                    }
                ],
                "as": "logs"
            }
        },
        {
            "$project": {
                "date": 1,
                "is_working_day": {"$lte": ["$dayOfWeek", 5]},
                "headcount": {"$size": "$active_emps"},
                "present_count": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {
                                "$cond": [
                                    {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$cond": ["$$l.half_day", 0.5, 1.0]},
                                    0.0
                                ]
                            }
                        }
                    }
                },
                "late_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "l",
                            "cond": {"$gt": ["$$l.late_minutes", 0]}
                        }
                    }
                }
            }
        },
        {
            "$project": {
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                "$is_working_day",
                                {"$gt": ["$headcount", 0]}
                            ]
                        },
                        {
                            "$round": [
                                {"$divide": ["$present_count", "$headcount"]},
                                4
                            ]
                        },
                        None
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {
                            "documents": [-6, 0]
                        }
                    }
                }
            }
        },
        {
            "$project": {
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": {
                    "$cond": [
                        {"$ne": ["$moving_avg_7d", None]},
                        {"$round": ["$moving_avg_7d", 4]},
                        None
                    ]
                }
            }
        },
        {"$sort": {"date": 1}}
    ]

    items = list(db.employees.aggregate(pipeline))
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Admin Explain Endpoint
# --------------------------------------------------------------------------- #
@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str = Path(..., pattern=r"^(attendance_list|employee_monthly|department_summary|late_leaderboard|department_trend)$"),
    emp_code: Optional[str] = Query(default=None),
    month: Optional[str] = Query(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = Query(default=None),
    limit: Optional[int] = Query(default=10, ge=1, le=50),
    date_from: Optional[str] = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status: Optional[str] = Query(default=None, pattern=r"^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$"),
    from_date: Optional[str] = Query(default=None, alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: Optional[str] = Query(default=None, alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        if date_from and date_to and date_from > date_to:
            raise HTTPException(status_code=422, detail="date_from must be <= date_to")
        q = {}
        if emp_code:
            q["emp_code"] = emp_code
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to
        if status:
            q["status"] = status
        skip = (page - 1) * page_size
        cmd = {
            "find": "attendance_logs",
            "filter": q,
            "sort": {"date": -1, "emp_code": 1},
            "skip": skip,
            "limit": page_size
        }
        raw_explain = db.command("explain", cmd, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": raw_explain}

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(status_code=422, detail="emp_code and month are required for employee_monthly")
        month_start, month_end, _ = get_month_range(month)
        pipeline = [
            {
                "$match": {
                    "emp_code": emp_code,
                    "date": {"$gte": month_start, "$lte": month_end}
                }
            },
            {
                "$project": {
                    "date": 1,
                    "status": 1,
                    "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                    "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]},
                    "half_day": {"$ifNull": ["$half_day", False]},
                    "dayOfWeek": {"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date"}}}
                }
            },
            {
                "$group": {
                    "_id": None,
                    "total_late_minutes": {"$sum": "$late_minutes"}
                }
            }
        ]
        cmd = {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}
        raw_explain = db.command("explain", cmd, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": raw_explain}

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(status_code=422, detail="month is required for department_summary")
        month_start, month_end, _ = get_month_range(month)
        match_stage = {"joined_on": {"$lte": month_end}}
        if department:
            match_stage["department"] = department
        pipeline = [
            {"$match": match_stage},
            {"$group": {"_id": "$department", "headcount": {"$sum": 1}}}
        ]
        cmd = {"aggregate": "employees", "pipeline": pipeline, "cursor": {}}
        raw_explain = db.command("explain", cmd, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "employees", "explain": raw_explain}

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(status_code=422, detail="month is required for late_leaderboard")
        month_start, month_end, _ = get_month_range(month)
        pipeline = [
            {
                "$match": {
                    "date": {"$gte": month_start, "$lte": month_end},
                    "late_minutes": {"$gt": 0}
                }
            },
            {
                "$group": {
                    "_id": "$emp_code",
                    "total_late_minutes": {"$sum": "$late_minutes"},
                    "late_count": {"$sum": 1}
                }
            }
        ]
        cmd = {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}
        raw_explain = db.command("explain", cmd, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": raw_explain}

    elif endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(status_code=422, detail="department, from, and to are required for department_trend")
        d_from = date.fromisoformat(from_date)
        d_to = date.fromisoformat(to_date)
        if d_to < d_from:
            raise HTTPException(status_code=422, detail="'to' date must be >= 'from' date")
        from_dt = datetime(d_from.year, d_from.month, d_from.day, 0, 0, 0, tzinfo=timezone.utc)
        pipeline = [
            {"$match": {"department": department}},
            {"$limit": 1}
        ]
        cmd = {"aggregate": "employees", "pipeline": pipeline, "cursor": {}}
        raw_explain = db.command("explain", cmd, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "employees", "explain": raw_explain}

    raise HTTPException(status_code=422, detail="Invalid endpoint")
