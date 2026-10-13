import pytest
import concurrent.futures
from datetime import datetime, timezone
from fastapi.testclient import TestClient
from app.main import app, db, init_db

test_client = TestClient(app)


def test_employee_with_no_logs():
    # Create employee with 0 logs
    emp_code = "EMP8001"
    dept = "NoLogDept"
    db.employees.delete_one({"emp_code": emp_code})
    db.attendance_logs.delete_many({"emp_code": emp_code})

    resp = test_client.post("/employees", json={
        "emp_code": emp_code,
        "name": "Ghost Employee",
        "email": "ghost@example.com",
        "department": dept,
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-07-01"
    })
    assert resp.status_code == 201

    # 1. Department summary must count headcount = 1
    resp_dept = test_client.get(f"/analytics/departments/summary?month=2026-07&department={dept}")
    assert resp_dept.status_code == 200
    dept_items = resp_dept.json()["items"]
    assert len(dept_items) == 1
    assert dept_items[0]["headcount"] == 1
    assert dept_items[0]["present_days"] == 0.0
    assert dept_items[0]["avg_work_hours"] is None

    # 2. Department trend must have headcount = 1
    resp_trend = test_client.get(f"/analytics/departments/{dept}/trend?from=2026-07-01&to=2026-07-05")
    assert resp_trend.status_code == 200
    trend_items = resp_trend.json()["items"]
    assert len(trend_items) == 5
    for item in trend_items:
        assert item["headcount"] == 1
        assert item["present_count"] == 0.0
        if item["is_working_day"]:
            assert item["attendance_rate"] == 0.0
        else:
            assert item["attendance_rate"] is None

    # 3. Employee monthly summary
    resp_mon = test_client.get(f"/analytics/employees/{emp_code}/monthly?month=2026-07")
    assert resp_mon.status_code == 200
    data = resp_mon.json()
    assert data["working_days"] == 23  # July 2026 has 23 weekdays
    assert data["present_days"] == 0.0
    assert data["attendance_pct"] == 0.0


def test_leaderboard_ties_and_cutoff():
    dept = "TieDept"
    month = "2026-08"
    db.employees.delete_many({"department": dept})
    db.attendance_logs.delete_many({"emp_code": {"$regex": "^EMP700"}})

    # Create 3 employees
    for i in range(1, 4):
        code = f"EMP700{i}"
        test_client.post("/employees", json={
            "emp_code": code,
            "name": f"Tie User {i}",
            "email": f"tie{i}@example.com",
            "department": dept,
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01"
        })

    # EMP7001: 50 late minutes
    # EMP7002: 50 late minutes (tied with EMP7001)
    # EMP7003: 30 late minutes (3rd rank)
    db.attendance_logs.insert_many([
        {
            "emp_code": "EMP7001",
            "date": "2026-08-05",
            "status": "PRESENT",
            "late_minutes": 50,
            "history": []
        },
        {
            "emp_code": "EMP7002",
            "date": "2026-08-05",
            "status": "PRESENT",
            "late_minutes": 50,
            "history": []
        },
        {
            "emp_code": "EMP7003",
            "date": "2026-08-05",
            "status": "PRESENT",
            "late_minutes": 30,
            "history": []
        }
    ])

    # 1. Fetch leaderboard with limit=10 -> ranks 1, 1, 3
    resp = test_client.get(f"/analytics/leaderboard/late?month={month}&department={dept}&limit=10")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 3
    assert items[0]["emp_code"] == "EMP7001" and items[0]["rank"] == 1
    assert items[1]["emp_code"] == "EMP7002" and items[1]["rank"] == 1
    assert items[2]["emp_code"] == "EMP7003" and items[2]["rank"] == 3

    # 2. Limit=1: Both EMP7001 and EMP7002 share rank 1 <= limit 1, so both returned!
    resp_lim1 = test_client.get(f"/analytics/leaderboard/late?month={month}&department={dept}&limit=1")
    assert resp_lim1.status_code == 200
    lim1_items = resp_lim1.json()["items"]
    assert len(lim1_items) == 2
    assert lim1_items[0]["rank"] == 1
    assert lim1_items[1]["rank"] == 1


def test_weekend_records_r7():
    emp_code = "EMP6001"
    db.employees.delete_one({"emp_code": emp_code})
    db.attendance_logs.delete_many({"emp_code": emp_code})

    test_client.post("/employees", json={
        "emp_code": emp_code,
        "name": "Weekend Worker",
        "email": "wk@example.com",
        "department": "Support",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-07-01"
    })

    # 2026-07-11 is Saturday, 2026-07-12 is Sunday
    db.attendance_logs.insert_many([
        {
            "emp_code": emp_code,
            "date": "2026-07-11",
            "status": "PRESENT",
            "work_hours": 8.0,
            "late_minutes": 20,
            "overtime_minutes": 45,
            "half_day": False,
            "history": []
        },
        {
            "emp_code": emp_code,
            "date": "2026-07-12",
            "status": "PRESENT",
            "work_hours": 8.0,
            "late_minutes": 15,
            "overtime_minutes": 30,
            "half_day": False,
            "history": []
        }
    ])

    resp = test_client.get(f"/analytics/employees/{emp_code}/monthly?month=2026-07")
    assert resp.status_code == 200
    data = resp.json()
    # present_days only counts Mon-Fri records
    assert data["present_days"] == 0.0
    # weekend records still contribute to late and overtime
    assert data["late_count"] == 2
    assert data["total_late_minutes"] == 35
    assert data["total_overtime_minutes"] == 75


def test_mid_month_joiner_r7():
    emp_code = "EMP5001"
    db.employees.delete_one({"emp_code": emp_code})
    db.attendance_logs.delete_many({"emp_code": emp_code})

    # Joined on 2026-07-20 (Monday)
    test_client.post("/employees", json={
        "emp_code": emp_code,
        "name": "Mid Month Joiner",
        "email": "mid@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-07-20"
    })

    # From 2026-07-20 to 2026-07-31:
    # Week 1: 20, 21, 22, 23, 24 (5 days)
    # Week 2: 27, 28, 29, 30, 31 (5 days) -> total 10 working days
    resp = test_client.get(f"/analytics/employees/{emp_code}/monthly?month=2026-07")
    assert resp.status_code == 200
    assert resp.json()["working_days"] == 10


def test_concurrency_punch_in_race():
    # 5 concurrent requests with same emp_code and time
    emp_code = "EMP4001"
    db.employees.delete_one({"emp_code": emp_code})
    db.attendance_logs.delete_many({"emp_code": emp_code})

    test_client.post("/employees", json={
        "emp_code": emp_code,
        "name": "Race User",
        "email": "race@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01"
    })

    req_time = int(datetime(2026, 9, 1, 4, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)

    def do_punch():
        c = TestClient(app)
        return c.post("/attendance/punch-in", json={"emp_code": emp_code, "punched_at": req_time}).status_code

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        statuses = list(executor.map(lambda _: do_punch(), range(5)))

    assert statuses.count(201) == 1
    assert statuses.count(409) == 4
