"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience; real environment variables win)

The file reads top to bottom:
  1. setup (Mongo, constants, indexes)      4. serializers
  2. validation helpers and models          5. employees and attendance endpoints
  3. time handling and rules R1 - R5        6. analytics endpoints, then the explain endpoint
"""
import calendar
import json
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from datetime import date as Date
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()  # does not override variables that are already set in the environment

log = logging.getLogger("attendance")


# --------------------------------------------------------------------------- #
# 1. Setup
# --------------------------------------------------------------------------- #
# tz_aware=True: every datetime read from Mongo is an aware UTC datetime, never a naive one.
client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True, serverSelectionTimeoutMS=5000)
db = client[os.getenv("MONGO_DB", "attendance_db")]
employees = db["employees"]
logs = db["attendance_logs"]

IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
ONE_SECOND = timedelta(seconds=1)
ONE_MS = timedelta(milliseconds=1)

GRACE_SECONDS = 10 * 60  # R2
MIN_OVERTIME_MINUTES = 30  # R3
HALF_DAY_BELOW_HOURS = 4.5  # R5
MAX_SHIFT_SPAN = timedelta(hours=24)
MAX_TREND_DAYS = 92

PRESENCE = ["PRESENT", "WFH", "ON_DUTY"]  # R6

Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]
# strict: floats, strings and booleans are rejected; the bounds reject values that look like seconds.
EpochMs = Annotated[int, Field(strict=True, ge=100_000_000_000, le=4_102_444_800_000)]

HHMM_PATTERN = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"
DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
MONTH_RE = re.compile(r"^[0-9]{4}-(0[1-9]|1[0-2])$")


def ensure_indexes() -> None:
    """Idempotent: creating an index that already exists is a no-op."""
    employees.create_index([("emp_code", ASCENDING)], unique=True, name="uniq_emp_code")
    employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="dept_emp_code")
    employees.create_index([("joined_on", ASCENDING)], name="joined_on")
    logs.create_index([("emp_code", ASCENDING), ("date", DESCENDING)], unique=True, name="uniq_emp_date")
    logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="date_emp")


@asynccontextmanager
async def lifespan(_: FastAPI):
    for attempt in range(1, 4):  # MongoDB may still be starting
        try:
            ensure_indexes()
            break
        except PyMongoError:
            log.exception("could not create indexes (attempt %s/3)", attempt)
            time.sleep(1)
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


@app.exception_handler(PyMongoError)
async def mongo_error_handler(_: Request, exc: PyMongoError):
    log.error("database error: %s", exc)
    return JSONResponse(status_code=503, content={"detail": "database error"})


# --------------------------------------------------------------------------- #
# 2. Validation helpers and request models
# --------------------------------------------------------------------------- #
def invalid(msg: str, field: str = "body") -> HTTPException:
    """422 in the same shape as FastAPI's own validation errors."""
    return HTTPException(422, [{"loc": [field], "msg": msg, "type": "value_error"}])


def parse_date(value: str, field: str) -> Date:
    if not DATE_RE.match(value):
        raise invalid("must be a date in YYYY-MM-DD format", field)
    try:
        return Date.fromisoformat(value)
    except ValueError:
        raise invalid("not a real calendar date", field) from None


def parse_month(value: str) -> tuple[str, str, list[str]]:
    """'YYYY-MM' -> (first day, last day, every Mon-Fri date of the month), all as YYYY-MM-DD strings."""
    if not MONTH_RE.match(value):
        raise invalid("must be a month in YYYY-MM format", "month")
    year, month = int(value[:4]), int(value[5:])
    try:
        first = Date(year, month, 1)
        last = Date(year, month, calendar.monthrange(year, month)[1])
    except ValueError:
        raise invalid("not a real calendar month", "month") from None
    days = [first + timedelta(days=i) for i in range((last - first).days + 1)]
    return first.isoformat(), last.isoformat(), [d.isoformat() for d in days if d.weekday() < 5]


def parse_range(date_from: str, date_to: str) -> tuple[Date, Date]:
    lo, hi = parse_date(date_from, "from"), parse_date(date_to, "to")
    if hi < lo:
        raise invalid("to must not be before from", "to")
    if (hi - lo).days + 1 > MAX_TREND_DAYS:
        raise invalid(f"the range may not exceed {MAX_TREND_DAYS} days", "to")
    return lo, hi


def require(value, name: str):
    if value is None:
        raise invalid(f"{name} is required", name)
    return value


def round_half_up(value: Decimal, places: int) -> float:
    """R8: round half-up (Python's round() is banker's rounding) and return a float."""
    return float(value.quantize(Decimal(10) ** -places, rounding=ROUND_HALF_UP))


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP[0-9]{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=HHMM_PATTERN)
    shift_end: str = Field("18:30", pattern=HHMM_PATTERN)
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def _joined_on_is_a_date(cls, v: str) -> str:
        if not DATE_RE.match(v):
            raise ValueError("must be YYYY-MM-DD")
        Date.fromisoformat(v)
        return v

    @model_validator(mode="after")
    def _shift_times_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self


class PunchInIn(BaseModel):
    emp_code: str = Field(min_length=1)
    punched_at: Optional[EpochMs] = None
    status: PresenceStatus = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str = Field(min_length=1)
    punched_at: Optional[EpochMs] = None


class RegularizeIn(BaseModel):
    status: Optional[Status] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# 3. Time handling and the business rules R1 - R5
# --------------------------------------------------------------------------- #
def from_ms(ms: int) -> datetime:
    """Epoch milliseconds -> aware UTC datetime truncated to whole seconds (R1)."""
    return (EPOCH + timedelta(milliseconds=ms)).replace(microsecond=0)


def to_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    return (dt - EPOCH) // ONE_MS


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def parse_hhmm(value: str) -> tuple[int, int]:
    hour, minute = value.split(":")
    return int(hour), int(minute)


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    """R1: the IST calendar date of the punch-in; an overnight shift's early-morning punch-in belongs to the day before."""
    local = punch_in.astimezone(IST)
    day = local.date()
    overnight = parse_hhmm(shift_end) <= parse_hhmm(shift_start)
    if overnight and (local.hour, local.minute, local.second) < (*parse_hhmm(shift_end), 0):
        day -= timedelta(days=1)
    return day.isoformat()


def shift_bounds(day: str, shift_start: str, shift_end: str) -> tuple[datetime, datetime]:
    """Start and end of the shift that belongs to attendance date `day`; an overnight shift ends the next day."""
    d = Date.fromisoformat(day)
    (sh, sm), (eh, em) = parse_hhmm(shift_start), parse_hhmm(shift_end)
    start = datetime(d.year, d.month, d.day, sh, sm, tzinfo=IST)
    end = datetime(d.year, d.month, d.day, eh, em, tzinfo=IST)
    if (eh, em) <= (sh, sm):
        end += timedelta(days=1)
    return start, end


def compute_late_minutes(punch_in: datetime, day: str, emp: dict) -> int:
    """R2: late only if strictly more than 10:00 after shift start; then whole minutes since shift start."""
    start, _ = shift_bounds(day, emp["shift_start"], emp["shift_end"])
    seconds = (punch_in - start) // ONE_SECOND
    return seconds // 60 if seconds > GRACE_SECONDS else 0


def compute_overtime(punch_out: datetime, day: str, emp: dict) -> int:
    """R3: whole minutes past shift end, counted only when at least 30."""
    _, end = shift_bounds(day, emp["shift_start"], emp["shift_end"])
    minutes = ((punch_out - end) // ONE_SECOND) // 60
    return minutes if minutes >= MIN_OVERTIME_MINUTES else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: (out - in) in hours, 2 decimals, half-up."""
    seconds = (punch_out - punch_in) // ONE_SECOND
    return round_half_up(Decimal(seconds) / 3600, 2)


def derive(status: str, punch_in: Optional[datetime], punch_out: Optional[datetime], day: str, emp: dict) -> dict:
    """The four stored derived fields for a record in the given state (DATA_MODEL.md section 3)."""
    if status not in PRESENCE or punch_in is None:
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    late = compute_late_minutes(punch_in, day, emp)
    if punch_out is None:
        return {"work_hours": None, "late_minutes": late, "overtime_minutes": 0, "half_day": False}
    work_hours = compute_work_hours(punch_in, punch_out)
    return {
        "work_hours": work_hours,
        "late_minutes": late,
        "overtime_minutes": compute_overtime(punch_out, day, emp),
        "half_day": work_hours < HALF_DAY_BELOW_HOURS,
    }


# --------------------------------------------------------------------------- #
# 4. Serializers: BSON -> API shape (datetimes become epoch ms, _id never leaves)
# --------------------------------------------------------------------------- #
def employee_out(doc: dict) -> dict:
    keys = ("emp_code", "name", "email", "department", "shift_start", "shift_end", "joined_on")
    return {**{k: doc[k] for k in keys}, "created_at": to_ms(doc.get("created_at"))}


def _api_value(value):
    return to_ms(value) if isinstance(value, datetime) else value


def record_out(doc: dict) -> dict:
    """Legacy documents may omit late_minutes, overtime_minutes, half_day and history: they read as defaults."""
    history = [
        {
            "at": to_ms(h["at"]),
            "by": h["by"],
            "reason": h["reason"],
            "changes": {f: {"from": _api_value(c["from"]), "to": _api_value(c["to"])} for f, c in h["changes"].items()},
        }
        for h in doc.get("history") or []
    ]
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": to_ms(doc.get("punch_in")),
        "punch_out": to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day")),
        "history": history,
    }


def get_employee(emp_code: str) -> dict:
    emp = employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    return emp


# --------------------------------------------------------------------------- #
# 5. Endpoints: system, employees, attendance
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(503, "database unavailable") from None
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = {**body.model_dump(), "created_at": now_utc()}
    try:
        employees.insert_one(doc)  # the unique index on emp_code decides, not a prior find_one
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists") from None
    return employee_out(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {"department": department} if department else {}
    cursor = employees.find(q).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)
    return {
        "items": [employee_out(d) for d in cursor],
        "total": employees.count_documents(q),
        "page": page,
        "page_size": page_size,
    }


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = get_employee(body.emp_code)
    ts = from_ms(body.punched_at) if body.punched_at is not None else now_utc()
    day = attendance_date(ts, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": day,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        **derive(body.status, ts, None, day, emp),  # work_hours None, late per R2, overtime 0, half_day False
        "history": [],
    }
    try:
        logs.insert_one(doc)  # unique (emp_code, date): of N simultaneous requests exactly one insert succeeds
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date") from None
    return record_out(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = get_employee(body.emp_code)
    ts = from_ms(body.punched_at) if body.punched_at is not None else now_utc()
    # Most recent record that started at or before the punch-out (date is unique per employee, so sorting by it
    # walks the (emp_code, date) index newest-first). ABSENT/LEAVE rows have a null punch_in and never match.
    rec = logs.find_one({"emp_code": body.emp_code, "punch_in": {"$lte": ts}}, sort=[("date", DESCENDING)])
    if rec is None:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    if ts <= rec["punch_in"]:
        raise invalid("punched_at must be after the punch-in", "punched_at")
    if ts - rec["punch_in"] > MAX_SHIFT_SPAN:
        raise invalid("punched_at is more than 24 hours after the punch-in", "punched_at")
    # The filter only matches while the record is still open, so a second simultaneous punch-out gets 409.
    updated = logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None},
        {"$set": {"punch_out": ts, **derive(rec["status"], rec["punch_in"], ts, rec["date"], emp)}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "already punched out")
    return record_out(updated)


def attendance_query(emp_code, date_from, date_to, status, page, page_size):
    """Filter, sort, skip and limit for GET /attendance. The explain endpoint reuses it."""
    q: dict = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        lo = parse_date(date_from, "date_from") if date_from else None
        hi = parse_date(date_to, "date_to") if date_to else None
        if lo and hi and lo > hi:
            raise invalid("date_from must not be after date_to", "date_from")
        q["date"] = {}
        if lo:
            q["date"]["$gte"] = lo.isoformat()
        if hi:
            q["date"]["$lte"] = hi.isoformat()
    if status:
        q["status"] = status
    sort = [("date", DESCENDING), ("emp_code", ASCENDING)]
    return q, sort, (page - 1) * page_size, page_size


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q, sort, skip, limit = attendance_query(emp_code, date_from, date_to, status, page, page_size)
    items = [record_out(d) for d in logs.find(q).sort(sort).skip(skip).limit(limit)]
    return {"items": items, "total": logs.count_documents(q), "page": page, "page_size": page_size}


# ---- regularization (PATCH) ----
TRACKED_FIELDS = ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day")


def corrected_times(emp: dict, rec: dict, body: RegularizeIn) -> tuple[str, Optional[datetime], Optional[datetime]]:
    """Status and punch times after the correction. Anything not sent keeps its stored value. Raises 422 on a rule violation."""
    status = body.status or rec["status"]
    sent_in, sent_out = body.punch_in is not None, body.punch_out is not None
    if status not in PRESENCE:  # ABSENT / LEAVE clear both punch times, so sending one is a contradiction
        if sent_in or sent_out:
            raise invalid("ABSENT and LEAVE clear the punch times; do not send them", "punch_in")
        return status, None, None
    punch_in = from_ms(body.punch_in) if sent_in else rec.get("punch_in")
    punch_out = from_ms(body.punch_out) if sent_out else rec.get("punch_out")
    if punch_in is None:
        raise invalid("a presence status needs a punch_in", "punch_in")
    if sent_in and attendance_date(punch_in, emp["shift_start"], emp["shift_end"]) != rec["date"]:
        raise invalid("punch_in must stay on the record's attendance date", "punch_in")
    if punch_out is not None and (sent_in or sent_out):
        if punch_out <= punch_in:
            raise invalid("punch_out must be after punch_in", "punch_out")
        if punch_out - punch_in > MAX_SHIFT_SPAN:
            raise invalid("punch_out must be within 24 hours of punch_in", "punch_out")
    return status, punch_in, punch_out


def changed_fields(rec: dict, new: dict) -> dict:
    """{field: {"from": old, "to": new}} for the tracked fields that really changed (missing legacy values read as defaults)."""
    old = {
        "status": rec["status"],
        "punch_in": rec.get("punch_in"),
        "punch_out": rec.get("punch_out"),
        "work_hours": rec.get("work_hours"),
        "late_minutes": rec.get("late_minutes") or 0,
        "overtime_minutes": rec.get("overtime_minutes") or 0,
        "half_day": bool(rec.get("half_day")),
    }
    return {f: {"from": old[f], "to": new[f]} for f in TRACKED_FIELDS if old[f] != new[f]}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    parse_date(date, "date")
    emp = get_employee(emp_code)
    rec = logs.find_one({"emp_code": emp_code, "date": date})
    if rec is None:
        raise HTTPException(404, "no attendance record for that date")

    status, punch_in, punch_out = corrected_times(emp, rec, body)
    new = {"status": status, "punch_in": punch_in, "punch_out": punch_out, **derive(status, punch_in, punch_out, date, emp)}
    changes = changed_fields(rec, new)
    if not changes:
        raise invalid("the request changes nothing")
    entry = {"at": now_utc(), "by": body.regularized_by, "reason": body.reason, "changes": changes}

    # Write only if the record is still exactly as we read it: same status and punch times, and no history entry
    # added since (index n does not exist yet). If a concurrent correction got in first we match nothing and
    # answer 409, so two corrections can never overwrite each other or lose a history entry.
    still_as_read = {
        "emp_code": emp_code,
        "date": date,
        "status": rec["status"],
        "punch_in": rec.get("punch_in"),
        "punch_out": rec.get("punch_out"),
        f"history.{len(rec.get('history') or [])}": {"$exists": False},
    }
    updated = logs.find_one_and_update(
        still_as_read, {"$set": new, "$push": {"history": entry}}, return_document=ReturnDocument.AFTER
    )
    if updated is None:
        raise HTTPException(409, "the record was changed by someone else, retry")
    return record_out(updated)


# --------------------------------------------------------------------------- #
# 6. Analytics: aggregation pipelines (each builder is shared with the explain endpoint)
# --------------------------------------------------------------------------- #
def count_if(cond) -> dict:
    return {"$sum": {"$cond": [cond, 1, 0]}}


IS_PRESENT = {"$in": ["$status", PRESENCE]}
IS_LATE = {"$gt": ["$late_minutes", 0]}
# R5/R6: a present day counts 1, a half day 0.5, an absent / leave day 0
PRESENT_DAYS = {"$cond": [IS_PRESENT, {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]}, 0]}


def employee_monthly_pipeline(emp_code: str, first: str, last: str, weekdays: list[str]) -> list[dict]:
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first, "$lte": last}}},
        {
            "$group": {
                "_id": None,
                # present_days only counts Mon-Fri records; the other totals count every record (R7)
                "present": {"$sum": {"$cond": [{"$in": ["$date", weekdays]}, PRESENT_DAYS, 0]}},
                "leave_days": count_if({"$eq": ["$status", "LEAVE"]}),
                "late_count": count_if(IS_LATE),
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            }
        },
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(...)):
    first, last, weekdays = parse_month(month)
    emp = get_employee(emp_code)
    working_days = sum(1 for d in weekdays if d >= emp["joined_on"])  # counted from joined_on (R7)
    row = next(logs.aggregate(employee_monthly_pipeline(emp_code, first, last, weekdays)), {})
    present_days = float(row.get("present", 0))
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": row.get("leave_days", 0),
        "late_count": row.get("late_count", 0),
        "total_late_minutes": row.get("total_late_minutes", 0),
        "total_overtime_minutes": row.get("total_overtime_minutes", 0),
        "attendance_pct": round_half_up(Decimal(present_days) * 100 / working_days, 2) if working_days else None,
    }


def department_summary_pipeline(first: str, last: str, weekdays: list[str], department: Optional[str]) -> list[dict]:
    has_hours = {"$and": [IS_PRESENT, {"$isNumber": "$work_hours"}]}
    # Per employee: fold the month's logs into one row. Runs inside the $lookup, served by the (emp_code, date) index.
    per_employee = [
        {"$match": {"date": {"$gte": first, "$lte": last}}},
        {
            "$group": {
                "_id": None,
                "present": {"$sum": {"$cond": [{"$in": ["$date", weekdays]}, PRESENT_DAYS, 0]}},
                "hours_sum": {"$sum": {"$cond": [has_hours, "$work_hours", 0]}},
                "hours_n": count_if(has_hours),
                "late_count": count_if(IS_LATE),
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "leave_count": count_if({"$eq": ["$status", "LEAVE"]}),
                "on_duty_count": count_if({"$eq": ["$status", "ON_DUTY"]}),
            }
        },
    ]
    match: dict = {"joined_on": {"$lte": last}}  # R9
    if department:
        match["department"] = department
    # Starting from employees (not from the logs) is what makes people with zero logs count in the headcount.
    return [
        {"$match": match},
        {"$lookup": {"from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code",
                     "pipeline": per_employee, "as": "agg"}},
        {"$unwind": {"path": "$agg", "preserveNullAndEmptyArrays": True}},
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present": {"$sum": "$agg.present"},
                "hours_sum": {"$sum": "$agg.hours_sum"},
                "hours_n": {"$sum": "$agg.hours_n"},
                "late_count": {"$sum": "$agg.late_count"},
                "total_late_minutes": {"$sum": "$agg.total_late_minutes"},
                "leave_count": {"$sum": "$agg.leave_count"},
                "on_duty_count": {"$sum": "$agg.on_duty_count"},
            }
        },
        {"$sort": {"_id": 1}},
    ]


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(...), department: Optional[str] = None):
    first, last, weekdays = parse_month(month)
    items = []
    for r in employees.aggregate(department_summary_pipeline(first, last, weekdays, department)):
        # mean over records (not a mean of per-employee means); the float sum is cleaned to 6 places before dividing
        n = r["hours_n"]
        avg_hours = round_half_up(Decimal(f"{r['hours_sum']:.6f}") / n, 2) if n else None
        items.append({
            "department": r["_id"],
            "headcount": r["headcount"],
            "present_days": float(r["present"]),
            "avg_work_hours": avg_hours,
            "late_count": r["late_count"],
            "total_late_minutes": r["total_late_minutes"],
            "leave_count": r["leave_count"],
            "on_duty_count": r["on_duty_count"],
        })
    return {"month": month, "items": items}


def late_leaderboard_pipeline(first: str, last: str, limit: int, department: Optional[str]) -> list[dict]:
    pipeline: list[dict] = [
        {"$match": {"date": {"$gte": first, "$lte": last}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},  # logs of unknown employees drop out here
    ]
    if department:
        pipeline.append({"$match": {"emp.department": department}})
    pipeline += [
        # $rank is standard competition ranking: 1, 2, 2, 4
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},  # limit applies to the rank, after ranking
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$emp.name", "department": "$emp.department",
                      "total_late_minutes": 1, "late_count": 1}},
    ]
    return pipeline


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str = Query(...), limit: int = Query(10, ge=1, le=50), department: Optional[str] = None):
    first, last, _ = parse_month(month)
    return {"month": month, "items": list(logs.aggregate(late_leaderboard_pipeline(first, last, limit, department)))}


def department_trend_pipeline(department: str, lo: Date, hi: Date) -> list[dict]:
    """One row per calendar day from lo to hi. The stages, in order:
      1. employees of the department, each with their logs in the range ($lookup)
      2. turn that into "events": one "joined" event per employee plus one event per log
      3. group the events by date, then fill the missing dates ($densify)
      4. running totals and the 7-day window ($setWindowFields)
    """
    first, last = lo.isoformat(), hi.isoformat()
    day0 = datetime(lo.year, lo.month, lo.day, tzinfo=timezone.utc)
    day_after_last = day0 + timedelta(days=(hi - lo).days + 1)  # $densify's upper bound is exclusive
    is_weekday = {"$lte": [{"$isoDayOfWeek": "$day"}, 5]}
    # Attendance rate in ten-thousandths, rounded half-up with integer maths: floor(10000*present/headcount + 1/2).
    rate = {"$floor": {"$divide": [{"$add": [{"$multiply": ["$present", 20000]}, "$headcount"]},
                                   {"$multiply": ["$headcount", 2]}]}}
    return [
        {"$match": {"department": department}},
        {"$lookup": {"from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code", "as": "logs",
                     "pipeline": [
                         {"$match": {"date": {"$gte": first, "$lte": last}}},
                         {"$project": {"_id": 0, "date": 1, "joined": {"$literal": 0}, "present": PRESENT_DAYS,
                                       "late": {"$cond": [IS_LATE, 1, 0]}}},
                     ]}},
        # The joined event sits on joined_on, clamped into [lo, hi]: everyone employed before `lo` counts from `lo`,
        # someone joining after `hi` contributes 0.
        {"$project": {"_id": 0, "events": {"$concatArrays": [
            [{"date": {"$cond": [{"$or": [{"$lt": ["$joined_on", first]}, {"$gt": ["$joined_on", last]}]}, first, "$joined_on"]},
              "joined": {"$cond": [{"$gt": ["$joined_on", last]}, 0, 1]}, "present": 0, "late": 0}],
            "$logs",
        ]}}},
        {"$unwind": "$events"},
        {"$group": {"_id": "$events.date", "joined": {"$sum": "$events.joined"}, "present": {"$sum": "$events.present"},
                    "late": {"$sum": "$events.late"}}},
        {"$project": {"_id": 0, "joined": 1, "present": 1, "late": 1,
                      "day": {"$dateFromString": {"dateString": "$_id", "format": "%Y-%m-%d", "timezone": "UTC"}}}},
        {"$sort": {"day": 1}},
        {"$densify": {"field": "day", "range": {"step": 1, "unit": "day", "bounds": [day0, day_after_last]}}},
        {"$set": {"joined": {"$ifNull": ["$joined", 0]}, "present": {"$ifNull": ["$present", 0]},
                  "late": {"$ifNull": ["$late", 0]}}},
        # headcount = running total of joiners
        {"$setWindowFields": {"sortBy": {"day": 1},
                              "output": {"headcount": {"$sum": "$joined", "window": {"documents": ["unbounded", "current"]}}}}},
        {"$set": {"rate": {"$cond": [{"$and": [is_weekday, {"$gt": ["$headcount", 0]}]}, rate, None]}}},
        # mean of the non-null rates in this row and the 6 before it ($avg skips nulls, and is null if all are)
        {"$setWindowFields": {"sortBy": {"day": 1}, "output": {"moving": {"$avg": "$rate", "window": {"documents": [-6, 0]}}}}},
        {"$sort": {"day": 1}},
        {"$project": {"_id": 0, "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$day", "timezone": "UTC"}},
                      "working": is_weekday, "headcount": 1, "present": 1, "late": 1, "rate": 1, "moving": 1}},
    ]


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, date_from: str = Query(..., alias="from"), date_to: str = Query(..., alias="to")):
    lo, hi = parse_range(date_from, date_to)
    if employees.find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(404, "unknown department")
    items = [
        {
            "date": r["date"],
            "is_working_day": r["working"],
            "headcount": r["headcount"],
            "present_count": float(r["present"]),
            "late_count": r["late"],
            "attendance_rate": None if r["rate"] is None else int(r["rate"]) / 10000,
            "moving_avg_7d": None if r["moving"] is None else round_half_up(Decimal(r["moving"]) / 10000, 4),
        }
        for r in employees.aggregate(department_trend_pipeline(department, lo, hi))
    ]
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Explain: the same query / pipeline builders as the endpoints, run through explain
# --------------------------------------------------------------------------- #
@app.get("/admin/explain/{endpoint}")
def explain(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        q, sort, skip, lim = attendance_query(emp_code, date_from, date_to, status, page, page_size)
        collection = "attendance_logs"
        command = {"find": collection, "filter": q, "sort": dict(sort), "skip": skip, "limit": lim}
    else:
        if endpoint == "employee_monthly":
            first, last, weekdays = parse_month(require(month, "month"))
            collection, pipeline = "attendance_logs", employee_monthly_pipeline(require(emp_code, "emp_code"), first, last, weekdays)
        elif endpoint == "department_summary":
            first, last, weekdays = parse_month(require(month, "month"))
            collection, pipeline = "employees", department_summary_pipeline(first, last, weekdays, department)
        elif endpoint == "late_leaderboard":
            first, last, _ = parse_month(require(month, "month"))
            collection, pipeline = "attendance_logs", late_leaderboard_pipeline(first, last, limit, department)
        else:  # department_trend
            lo, hi = parse_range(require(from_, "from"), require(to, "to"))
            collection, pipeline = "employees", department_trend_pipeline(require(department, "department"), lo, hi)
        command = {"aggregate": collection, "pipeline": pipeline, "cursor": {}}
    raw = db.command("explain", command, verbosity="executionStats")
    return {
        "endpoint": endpoint,
        "collection": collection,
        "explain": json.loads(json_util.dumps(raw, json_options=json_util.RELAXED_JSON_OPTIONS)),
    }
