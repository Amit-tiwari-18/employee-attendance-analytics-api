"""
Employee Attendance & Analytics API - HROne assignment.
Run: uvicorn app.main:app --port 8000
Configuration: MONGO_URI and MONGO_DB; a local .env is loaded for convenience.
"""
import os
import re
import calendar
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Literal, Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Path as ApiPath
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pymongo import MongoClient, ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError
from bson import json_util

load_dotenv()
client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), serverSelectionTimeoutMS=5000)
db = client[os.getenv("MONGO_DB", "attendance_db")]
IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
PRESENCE_STATUSES = {"PRESENT", "WFH", "ON_DUTY"}
ALL_STATUSES = PRESENCE_STATUSES | {"ABSENT", "LEAVE"}
MIN_EPOCH_MS = 100_000_000_000
MAX_EPOCH_MS = 4_102_444_800_000
app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")

# --------------------------------------------------------------------------- #
# MongoDB indexes: safe to create repeatedly and enforced by the database.
# --------------------------------------------------------------------------- #
def ensure_indexes():
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_employee_code")
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="department_joined")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uq_attendance_employee_date")
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="attendance_date_employee")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING), ("status", ASCENDING)], name="attendance_employee_date_status")
    db.attendance_logs.create_index([("date", ASCENDING), ("late_minutes", DESCENDING), ("emp_code", ASCENDING)], name="attendance_date_late")

@app.on_event("startup")
def startup_indexes():
    ensure_indexes()

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def truncate_seconds(dt: datetime) -> datetime:
    return dt.replace(microsecond=0)

def epoch_to_datetime(value: Optional[int], *, default_now: bool = False) -> datetime:
    if value is None:
        if default_now:
            return truncate_seconds(datetime.now(UTC).replace(tzinfo=None))
        raise ValueError("timestamp is required")
    if isinstance(value, bool) or not isinstance(value, int) or value < MIN_EPOCH_MS or value > MAX_EPOCH_MS:
        raise HTTPException(422, "timestamp must be an integer epoch-millisecond value in the supported range")
    try:
        return truncate_seconds(datetime.fromtimestamp(value / 1000, tz=UTC).replace(tzinfo=None))
    except (OverflowError, OSError, ValueError):
        raise HTTPException(422, "timestamp is out of range")

def datetime_to_epoch(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    # PyMongo returns naive UTC datetimes by default.
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp() * 1000)

def round_half_up(value: float, places: int = 2) -> float:
    quant = Decimal("1") if places == 0 else Decimal("1").scaleb(-places)
    return float(Decimal(str(value)).quantize(quant, rounding=ROUND_HALF_UP))

def parse_date(value: str, field: str = "date") -> date:
    try:
        parsed = date.fromisoformat(value)
        if parsed.isoformat() != value:
            raise ValueError
        return parsed
    except (ValueError, TypeError):
        raise HTTPException(422, f"{field} must be YYYY-MM-DD")

def parse_month(value: str) -> tuple[date, date]:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value):
        raise HTTPException(422, "month must be YYYY-MM")
    year, month = map(int, value.split("-"))
    first = date(year, month, 1)
    last = date(year, month, calendar.monthrange(year, month)[1])
    return first, last

def valid_time(value: str) -> bool:
    return bool(re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value or ""))

def shift_datetimes(attendance_date: str, shift_start: str, shift_end: str) -> tuple[datetime, datetime]:
    day = parse_date(attendance_date)
    sh, sm = map(int, shift_start.split(":"))
    eh, em = map(int, shift_end.split(":"))
    start = datetime.combine(day, datetime.min.time()).replace(hour=sh, minute=sm, tzinfo=IST)
    end_day = day + timedelta(days=1) if shift_end <= shift_start else day
    end = datetime.combine(end_day, datetime.min.time()).replace(hour=eh, minute=em, tzinfo=IST)
    return start, end

def attendance_date_for(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    local = punch_in.replace(tzinfo=UTC).astimezone(IST)
    if shift_end <= shift_start:
        end_h, end_m = map(int, shift_end.split(":"))
        if (local.hour, local.minute) < (end_h, end_m):
            local -= timedelta(days=1)
    return local.date().isoformat()

def compute_late_minutes(punch_in: datetime, shift_start: str, attendance_date: Optional[str] = None, shift_end: Optional[str] = None) -> int:
    local_in = punch_in.replace(tzinfo=UTC).astimezone(IST) if punch_in.tzinfo is None else punch_in.astimezone(IST)
    d = attendance_date or local_in.date().isoformat()
    start, _ = shift_datetimes(d, shift_start, shift_end or "23:59")
    # Compare UTC instants; shift start is IST.
    elapsed = (local_in - start).total_seconds()
    return int(elapsed // 60) if elapsed > 600 else 0

def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = (punch_out - punch_in).total_seconds()
    return round_half_up(seconds / 3600, 2)

def compute_overtime(punch_out: datetime, shift_end: str, date_str: str, shift_start: Optional[str] = None) -> int:
    if shift_start is None:
        # Preserve the original helper's call signature for callers that only know date and end.
        shift_start = "00:00"
    _, end = shift_datetimes(date_str, shift_start, shift_end)
    local_out = punch_out.replace(tzinfo=UTC).astimezone(IST) if punch_out.tzinfo is None else punch_out.astimezone(IST)
    minutes = int((local_out - end).total_seconds() // 60)
    return minutes if minutes >= 30 else 0

def serialize_history(history: list) -> list:
    result = []
    for entry in history or []:
        changes = {}
        for key, pair in (entry.get("changes") or {}).items():
            changes[key] = {"from": datetime_to_epoch(pair.get("from")) if isinstance(pair.get("from"), datetime) else pair.get("from"),
                            "to": datetime_to_epoch(pair.get("to")) if isinstance(pair.get("to"), datetime) else pair.get("to")}
        result.append({"at": datetime_to_epoch(entry.get("at")), "by": entry.get("by"), "reason": entry.get("reason"), "changes": changes})
    return result

def serialize_attendance(doc: dict) -> dict:
    return {
        "emp_code": doc.get("emp_code"), "date": doc.get("date"), "status": doc.get("status"),
        "punch_in": datetime_to_epoch(doc.get("punch_in")), "punch_out": datetime_to_epoch(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"), "late_minutes": int(doc.get("late_minutes", 0) or 0),
        "overtime_minutes": int(doc.get("overtime_minutes", 0) or 0), "half_day": bool(doc.get("half_day", False)),
        "history": serialize_history(doc.get("history", [])),
    }

def serialize_employee(doc: dict) -> dict:
    result = {k: v for k, v in doc.items() if k != "_id"}
    if isinstance(result.get("created_at"), datetime):
        result["created_at"] = datetime_to_epoch(result["created_at"])
    return result

def require_employee(emp_code: str) -> dict:
    emp = db.employees.find_one({"emp_code": emp_code}, {"_id": 0})
    if not emp:
        raise HTTPException(404, "employee not found")
    return emp

def working_days(start: date, end: date) -> int:
    if end < start:
        return 0
    total = 0
    day = start
    while day <= end:
        if day.weekday() < 5:
            total += 1
        day += timedelta(days=1)
    return total

def mongo_json(value: Any) -> Any:
    return json_util.loads(json_util.dumps(value))

def aggregate_explain(collection, pipeline: list) -> dict:
    return collection.database.command({"explain": {"aggregate": collection.name, "pipeline": pipeline, "cursor": {}}, "verbosity": "executionStats"})

# --------------------------------------------------------------------------- #
# Models - retain the starter's request model approach, with contract validation.
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str = Field(min_length=1, max_length=50)
    name: str = Field(min_length=1)
    email: str = Field(min_length=3)
    department: str = Field(min_length=1)
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str

    @field_validator("shift_start", "shift_end")
    @classmethod
    def check_shift_time(cls, value):
        if not valid_time(value):
            raise ValueError("time must be HH:MM")
        return value

    @field_validator("joined_on")
    @classmethod
    def check_joined_on(cls, value):
        date.fromisoformat(value)
        if date.fromisoformat(value).isoformat() != value:
            raise ValueError("joined_on must be YYYY-MM-DD")
        return value

    @model_validator(mode="after")
    def shifts_must_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self

class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str = Field(min_length=1)
    punched_at: Optional[int] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"

    @field_validator("punched_at")
    @classmethod
    def validate_timestamp(cls, value):
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or not MIN_EPOCH_MS <= value <= MAX_EPOCH_MS):
            raise ValueError("punched_at must be epoch milliseconds")
        return value

class PunchOutIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str = Field(min_length=1)
    punched_at: Optional[int] = None

    @field_validator("punched_at")
    @classmethod
    def validate_timestamp(cls, value):
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or not MIN_EPOCH_MS <= value <= MAX_EPOCH_MS):
            raise ValueError("punched_at must be epoch milliseconds")
        return value

class RegularizeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @field_validator("punch_in", "punch_out")
    @classmethod
    def validate_timestamp(cls, value):
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or not MIN_EPOCH_MS <= value <= MAX_EPOCH_MS):
            raise ValueError("punch times must be epoch milliseconds")
        return value

# --------------------------------------------------------------------------- #
# Existing endpoints, corrected only where the contract requires it.
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except PyMongoError:
        raise HTTPException(503, "MongoDB unavailable")

@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(UTC).replace(tzinfo=None, microsecond=0)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return serialize_employee(doc)

@app.get("/employees")
def list_employees(department: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    q = {"department": department} if department is not None else {}
    total = db.employees.count_documents(q)
    items = list(db.employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size))
    return {"items": [serialize_employee(x) for x in items], "total": total, "page": page, "page_size": page_size}

@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = require_employee(body.emp_code)
    ts = epoch_to_datetime(body.punched_at, default_now=True)
    d = attendance_date_for(ts, emp["shift_start"], emp["shift_end"])
    late = compute_late_minutes(ts, emp["shift_start"], d, emp["shift_end"])
    doc = {"emp_code": body.emp_code, "date": d, "status": body.status, "punch_in": ts, "punch_out": None,
           "work_hours": None, "late_minutes": late, "overtime_minutes": 0, "half_day": False, "history": []}
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return serialize_attendance(doc)

@app.get("/attendance")
def list_attendance(emp_code: Optional[str] = None, date_from: Optional[str] = None, date_to: Optional[str] = None,
                    status: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    if date_from:
        parse_date(date_from, "date_from")
    if date_to:
        parse_date(date_to, "date_to")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must be on or before date_to")
    if status and status not in ALL_STATUSES:
        raise HTTPException(422, "invalid status")
    q = {}
    if emp_code: q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from: q["date"]["$gte"] = date_from
        if date_to: q["date"]["$lte"] = date_to
    if status: q["status"] = status
    total = db.attendance_logs.count_documents(q)
    docs = list(db.attendance_logs.find(q).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip((page - 1) * page_size).limit(page_size))
    return {"items": [serialize_attendance(d) for d in docs], "total": total, "page": page, "page_size": page_size}

# --------------------------------------------------------------------------- #
# Punch-out and manual regularization.
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = require_employee(body.emp_code)
    out_ts = epoch_to_datetime(body.punched_at, default_now=True)
    # The latest still-open record with punch-in <= punch-out time, including overnight shifts.
    doc = db.attendance_logs.find_one({"emp_code": body.emp_code, "punch_in": {"$ne": None, "$lte": out_ts}}, sort=[("punch_in", DESCENDING)])
    if not doc:
        raise HTTPException(404, "no punch-in found")
    if doc.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    seconds = (out_ts - doc["punch_in"]).total_seconds()
    if seconds <= 0 or seconds > 24 * 3600:
        raise HTTPException(422, "punched_at must be after punch_in and no more than 24 hours later")
    hours = compute_work_hours(doc["punch_in"], out_ts)
    overtime = compute_overtime(out_ts, emp["shift_end"], doc["date"], emp["shift_start"])
    half_day = hours < 4.50
    updated = db.attendance_logs.find_one_and_update(
        {"_id": doc["_id"], "punch_out": None},
        {"$set": {"punch_out": out_ts, "work_hours": hours, "overtime_minutes": overtime, "half_day": half_day}},
        return_document=ReturnDocument.AFTER)
    if updated is None:
        raise HTTPException(409, "already punched out")
    return serialize_attendance(updated)

@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    parse_date(date)
    emp = require_employee(emp_code)
    old = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if old is None:
        raise HTTPException(404, "attendance record not found")
    changes_requested = body.model_fields_set - {"reason", "regularized_by"}
    if not changes_requested:
        raise HTTPException(422, "request must change at least one field")
    new_status = body.status if "status" in changes_requested else old["status"]
    if new_status in {"ABSENT", "LEAVE"}:
        if "punch_in" in changes_requested and body.punch_in is not None or "punch_out" in changes_requested and body.punch_out is not None:
            raise HTTPException(422, "ABSENT and LEAVE cannot have punch times")
        new_in = new_out = None
    else:
        new_in = epoch_to_datetime(body.punch_in) if "punch_in" in changes_requested and body.punch_in is not None else old.get("punch_in")
        new_out = epoch_to_datetime(body.punch_out) if "punch_out" in changes_requested and body.punch_out is not None else old.get("punch_out")
        if new_in is None:
            raise HTTPException(422, "presence status requires punch_in")
        if attendance_date_for(new_in, emp["shift_start"], emp["shift_end"]) != date:
            raise HTTPException(422, "punch_in must belong to the record attendance date")
        if new_out is not None:
            seconds = (new_out - new_in).total_seconds()
            if seconds <= 0 or seconds > 24 * 3600:
                raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")
    if new_status in {"ABSENT", "LEAVE"}:
        late, hours, overtime, half = 0, None, 0, False
    else:
        late = compute_late_minutes(new_in, emp["shift_start"], date, emp["shift_end"])
        if new_out is None:
            hours, overtime, half = None, 0, False
        else:
            hours = compute_work_hours(new_in, new_out)
            overtime = compute_overtime(new_out, emp["shift_end"], date, emp["shift_start"])
            half = hours < 4.50
    new_values = {"status": new_status, "punch_in": new_in, "punch_out": new_out, "late_minutes": late,
                  "work_hours": hours, "overtime_minutes": overtime, "half_day": half}
    old_values = {k: old.get(k, False if k == "half_day" else 0 if k in ("late_minutes", "overtime_minutes") else None) for k in new_values}
    actual_changes = {}
    for key, value in new_values.items():
        before = old_values[key]
        if before != value:
            actual_changes[key] = {"from": before, "to": value}
    if not actual_changes:
        raise HTTPException(422, "request does not change the record")
    history_entry = {"at": datetime.now(UTC).replace(tzinfo=None, microsecond=0), "by": body.regularized_by,
                     "reason": body.reason, "changes": actual_changes}
    # Compare-and-swap update prevents concurrent corrections from overwriting one another's history.
    filt = {"_id": old["_id"], "status": old.get("status"), "punch_in": old.get("punch_in"), "punch_out": old.get("punch_out")}
    updated = db.attendance_logs.find_one_and_update(filt, {"$set": new_values, "$push": {"history": history_entry}}, return_document=ReturnDocument.AFTER)
    if updated is None:
        raise HTTPException(409, "record changed concurrently; retry the correction")
    return serialize_attendance(updated)

# --------------------------------------------------------------------------- #
# Aggregation pipeline builders shared with the explain endpoint.
# --------------------------------------------------------------------------- #
def month_bounds_strings(month: str):
    first, last = parse_month(month)
    return first.isoformat(), last.isoformat(), first, last

def present_day_expression():
    """Count presence as 1/0.5 only for Monday-Friday; weekends count as 0."""
    return {
        "$cond": [
            {
                "$and": [
                    {"$in": ["$status", list(PRESENCE_STATUSES)]},
                    {
                        "$in": [
                            {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
                            [2, 3, 4, 5, 6],
                        ]
                    },
                ]
            },
            {
                "$cond": [
                    {"$eq": [{"$ifNull": ["$half_day", False]}, True]},
                    0.5,
                    1,
                ]
            },
            0,
        ]
    }

def employee_monthly_pipeline(emp_code: str, first_s: str, last_s: str):
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first_s, "$lte": last_s}}},
        {"$group": {"_id": None,
            "present_days": {"$sum": present_day_expression()},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}}}}
    ]

@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    require_employee(emp_code)
    first_s, last_s, first, last = month_bounds_strings(month)
    result = list(db.attendance_logs.aggregate(employee_monthly_pipeline(emp_code, first_s, last_s)))
    stats = result[0] if result else {"present_days": 0, "leave_days": 0, "late_count": 0, "total_late_minutes": 0, "total_overtime_minutes": 0}
    joined = parse_date(db.employees.find_one({"emp_code": emp_code}, {"joined_on": 1, "_id": 0})["joined_on"])
    wd_start = max(first, joined)
    wd = working_days(wd_start, last) if wd_start <= last else 0
    pct = round_half_up(stats["present_days"] / wd * 100, 2) if wd else None
    return {"emp_code": emp_code, "month": month, "working_days": wd, "present_days": round_half_up(stats["present_days"], 2),
            "leave_days": stats["leave_days"], "late_count": stats["late_count"], "total_late_minutes": stats["total_late_minutes"],
            "total_overtime_minutes": stats["total_overtime_minutes"], "attendance_pct": pct}

def department_summary_pipeline(first_s: str, last_s: str, department: Optional[str] = None):
    match = {"joined_on": {"$lte": last_s}}
    if department is not None: match["department"] = department
    return [
        {"$match": match},
        {"$group": {"_id": "$department", "headcount": {"$sum": 1}, "codes": {"$push": "$emp_code"}}},
        {"$lookup": {"from": "attendance_logs", "let": {"codes": "$codes"}, "pipeline": [
            {"$match": {"date": {"$gte": first_s, "$lte": last_s}}},
            {"$match": {"$expr": {"$in": ["$emp_code", "$$codes"]}}},
            {"$group": {"_id": None,
                "present_days": {"$sum": present_day_expression()},
                "work_hours_sum": {"$sum": {"$cond": [{"$and": [{"$in": ["$status", list(PRESENCE_STATUSES)]}, {"$ne": ["$work_hours", None]}]}, "$work_hours", 0]}},
                "work_hours_count": {"$sum": {"$cond": [{"$and": [{"$in": ["$status", list(PRESENCE_STATUSES)]}, {"$ne": ["$work_hours", None]}]}, 1, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}}}}
        ], "as": "stats"}},
        {"$set": {"stats": {"$ifNull": [{"$arrayElemAt": ["$stats", 0]}, {"present_days": 0, "work_hours_sum": 0, "work_hours_count": 0, "late_count": 0, "total_late_minutes": 0, "leave_count": 0, "on_duty_count": 0}]}}},
        {"$project": {"_id": 0, "department": "$_id", "headcount": 1,
            "present_days": "$stats.present_days",
            "avg_work_hours": {"$cond": [{"$gt": ["$stats.work_hours_count", 0]}, {"$round": [{"$divide": ["$stats.work_hours_sum", "$stats.work_hours_count"]}, 2]}, None]},
            "late_count": "$stats.late_count", "total_late_minutes": "$stats.total_late_minutes", "leave_count": "$stats.leave_count", "on_duty_count": "$stats.on_duty_count"}},
        {"$sort": {"department": 1}}
    ]

@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"), department: Optional[str] = None):
    first_s, last_s, _, _ = month_bounds_strings(month)
    items = list(db.employees.aggregate(department_summary_pipeline(first_s, last_s, department)))
    items = [dict(x, present_days=round_half_up(x.get("present_days", 0), 2), avg_work_hours=round_half_up(x["avg_work_hours"], 2) if x.get("avg_work_hours") is not None else None) for x in items if x.get("headcount", 0) > 0]
    return {"month": month, "items": items}

def leaderboard_pipeline(first_s: str, last_s: str, department: Optional[str] = None, limit: int = 10):
    pipeline = [
        {"$match": {"date": {"$gte": first_s, "$lte": last_s}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
    ]
    if department is not None: pipeline.append({"$match": {"employee.department": department}})
    pipeline += [
        {"$set": {"emp_code": "$_id", "name": "$employee.name", "department": "$employee.department"}},
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": 1, "name": 1, "department": 1, "total_late_minutes": 1, "late_count": 1}}
    ]
    return pipeline

@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"), limit: int = Query(10, ge=1, le=50), department: Optional[str] = None):
    first_s, last_s, _, _ = month_bounds_strings(month)
    items = list(db.attendance_logs.aggregate(leaderboard_pipeline(first_s, last_s, department, limit)))
    return {"month": month, "items": items}

def department_trend_pipeline(department: str, from_s: str, to_s: str):
    # Convert the ISO calendar date to BSON Date before $densify; $densify's day
    # unit applies to dates, not strings. Convert back to YYYY-MM-DD at the end.
    start_dt = datetime.combine(parse_date(from_s), datetime.min.time())
    end_dt = datetime.combine(parse_date(to_s), datetime.min.time())
    return [
        {"$match": {"date": {"$gte": from_s, "$lte": to_s}}},
        {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
        {"$match": {"employee.department": department}},
        {"$group": {"_id": "$date",
            "present_count": {"$sum": {"$cond": [{"$in": ["$status", list(PRESENCE_STATUSES)]}, {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1]}, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}}},
        {"$project": {"_id": 0, "date": {"$dateFromString": {"dateString": "$_id"}}, "present_count": 1, "late_count": 1}},
        {"$sort": {"date": 1}},
        {"$densify": {"field": "date", "range": {"step": 1, "unit": "day", "bounds": [start_dt, end_dt + timedelta(days=1)]}}},
        {"$match": {"date": {"$gte": start_dt, "$lte": end_dt}}},
        {"$set": {"present_count": {"$ifNull": ["$present_count", 0]}, "late_count": {"$ifNull": ["$late_count", 0]},
                   "date_str": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date"}}}},
        {"$lookup": {"from": "employees", "let": {"day": "$date_str"}, "pipeline": [
            {"$match": {"department": department}}, {"$match": {"$expr": {"$lte": ["$joined_on", "$$day"]}}}, {"$count": "n"}
        ], "as": "hc"}},
        {"$set": {"headcount": {"$ifNull": [{"$arrayElemAt": ["$hc.n", 0]}, 0]},
                   "is_working_day": {"$and": [{"$gte": [{"$dayOfWeek": "$date"}, 2]}, {"$lte": [{"$dayOfWeek": "$date"}, 6]}]}}},
        {"$set": {"attendance_rate": {"$cond": [{"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]}, {"$round": [{"$divide": ["$present_count", "$headcount"]}, 4]}, None]}}},
        {"$setWindowFields": {"sortBy": {"date": 1}, "output": {"moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}}},
        {"$project": {"_id": 0, "date": "$date_str", "is_working_day": 1, "headcount": 1, "present_count": 1, "late_count": 1,
                       "attendance_rate": {"$cond": [{"$ne": ["$attendance_rate", None]}, {"$round": ["$attendance_rate", 4]}, None]},
                       "moving_avg_7d": {"$cond": [{"$ne": ["$moving_avg_7d", None]}, {"$round": ["$moving_avg_7d", 4]}, None]}}},
        {"$sort": {"date": 1}}
    ]

@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_date: str = Query(..., alias="from"), to_date: str = Query(..., alias="to")):
    start = parse_date(from_date, "from")
    end = parse_date(to_date, "to")
    if end < start: raise HTTPException(422, "to must be on or after from")
    if (end - start).days + 1 > 92: raise HTTPException(422, "date range cannot exceed 92 days")
    if not db.employees.find_one({"department": department}, {"_id": 1}): raise HTTPException(404, "department not found")
    pipeline = department_trend_pipeline(department, start.isoformat(), end.isoformat())
    items = list(db.attendance_logs.aggregate(pipeline, allowDiskUse=True))
    # Preserve attendance_rate as a fraction; round values half-up at the API boundary.
    for item in items:
        if item.get("attendance_rate") is not None: item["attendance_rate"] = round_half_up(item["attendance_rate"], 4)
        if item.get("moving_avg_7d") is not None: item["moving_avg_7d"] = round_half_up(item["moving_avg_7d"], 4)
        item["present_count"] = round_half_up(item.get("present_count", 0), 2)
    return {"department": department, "items": items}

# --------------------------------------------------------------------------- #
# Explain endpoint. The same builders are used by analytics explain calls.
# --------------------------------------------------------------------------- #
@app.get("/admin/explain/{endpoint}")
def explain_endpoint(endpoint: str, emp_code: Optional[str] = None, month: Optional[str] = None,
                     department: Optional[str] = None, limit: int = Query(10, ge=1, le=50),
                     date_from: Optional[str] = None, date_to: Optional[str] = None, status: Optional[str] = None,
                     from_date: Optional[str] = Query(None, alias="from"), to_date: Optional[str] = Query(None, alias="to"),
                     page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    if endpoint not in {"attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"}:
        raise HTTPException(422, "unsupported endpoint")
    try:
        if endpoint == "attendance_list":
            if date_from: parse_date(date_from, "date_from")
            if date_to: parse_date(date_to, "date_to")
            q = {}
            if emp_code: q["emp_code"] = emp_code
            if date_from or date_to:
                q["date"] = {}
                if date_from: q["date"]["$gte"] = date_from
                if date_to: q["date"]["$lte"] = date_to
            if status: q["status"] = status
            pipeline = [{"$match": q}, {"$sort": {"date": -1, "emp_code": 1}}, {"$skip": (page - 1) * page_size}, {"$limit": page_size}]
            explain = aggregate_explain(db.attendance_logs, pipeline); collection = "attendance_logs"
        else:
            if not month and endpoint != "department_trend": raise HTTPException(422, "month is required")
            if endpoint == "employee_monthly":
                if not emp_code: raise HTTPException(422, "emp_code is required")
                first_s, last_s, _, _ = month_bounds_strings(month)
                explain = aggregate_explain(db.attendance_logs, employee_monthly_pipeline(emp_code, first_s, last_s)); collection = "attendance_logs"
            elif endpoint == "department_summary":
                first_s, last_s, _, _ = month_bounds_strings(month)
                explain = aggregate_explain(db.employees, department_summary_pipeline(first_s, last_s, department)); collection = "employees"
            elif endpoint == "late_leaderboard":
                first_s, last_s, _, _ = month_bounds_strings(month)
                explain = aggregate_explain(db.attendance_logs, leaderboard_pipeline(first_s, last_s, department, limit)); collection = "attendance_logs"
            else:
                if not department or not from_date or not to_date: raise HTTPException(422, "department, from and to are required")
                start, end = parse_date(from_date, "from"), parse_date(to_date, "to")
                if end < start or (end - start).days + 1 > 92: raise HTTPException(422, "invalid date range")
                explain = aggregate_explain(db.attendance_logs, department_trend_pipeline(department, start.isoformat(), end.isoformat())); collection = "attendance_logs"
        return {"endpoint": endpoint, "collection": collection, "explain": mongo_json(explain)}
    except HTTPException:
        raise
    except PyMongoError as exc:
        raise HTTPException(503, f"MongoDB explain failed: {exc.__class__.__name__}")
