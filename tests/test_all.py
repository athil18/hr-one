import pytest
from datetime import datetime, timezone, timedelta
from fastapi.testclient import TestClient
from app.main import app, db, client, init_db

test_client = TestClient(app)


@pytest.fixture(autouse=True)
def setup_test_db():
    # Ensure indexes are initialized
    init_db()
    # Reset test data by re-seeding or cleaning
    yield


def test_health():
    resp = test_client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_create_and_list_employees():
    emp_payload = {
        "emp_code": "EMP9991",
        "name": "Test User",
        "email": "test@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01"
    }
    # Clean up if exists
    db.employees.delete_one({"emp_code": "EMP9991"})
    db.attendance_logs.delete_many({"emp_code": "EMP9991"})

    # Create
    resp = test_client.post("/employees", json=emp_payload)
    assert resp.status_code == 201
    data = resp.json()
    assert data["emp_code"] == "EMP9991"
    assert isinstance(data["created_at"], int)
    assert "_id" not in data
    assert "id" not in data

    # Duplicate create -> 409
    resp_dup = test_client.post("/employees", json=emp_payload)
    assert resp_dup.status_code == 409

    # Equal shift start and end -> 422
    bad_shift = dict(emp_payload, emp_code="EMP9992", shift_start="09:30", shift_end="09:30")
    resp_bad = test_client.post("/employees", json=bad_shift)
    assert resp_bad.status_code == 422

    # List employees
    resp_list = test_client.get("/employees?department=Engineering&page=1&page_size=10")
    assert resp_list.status_code == 200
    list_data = resp_list.json()
    assert list_data["total"] >= 1
    assert any(e["emp_code"] == "EMP9991" for e in list_data["items"])


def test_r2_late_minutes_and_punch_in():
    # EMP0001 has shift 09:30 - 18:30 IST (+05:30)
    # 09:30 IST is 04:00 UTC
    # 09:40:00 IST is 04:10:00 UTC (1783311000000 ms) -> late = 0
    # 09:40:01 IST is 04:10:01 UTC -> late = 10
    # 10:15:59 IST is 04:45:59 UTC -> late = 45

    db.attendance_logs.delete_many({"emp_code": "EMP0001", "date": "2026-08-01"})
    db.attendance_logs.delete_many({"emp_code": "EMP0001", "date": "2026-08-02"})
    db.attendance_logs.delete_many({"emp_code": "EMP0001", "date": "2026-08-03"})

    # 1. 09:40:00 IST on 2026-08-01 -> late = 0
    dt_exact_grace = datetime(2026, 8, 1, 4, 10, 0, tzinfo=timezone.utc)
    ms_exact = int(dt_exact_grace.timestamp() * 1000)
    resp1 = test_client.post("/attendance/punch-in", json={
        "emp_code": "EMP0001",
        "punched_at": ms_exact
    })
    assert resp1.status_code == 201
    assert resp1.json()["late_minutes"] == 0

    # Duplicate punch-in on same day -> 409
    resp_dup = test_client.post("/attendance/punch-in", json={
        "emp_code": "EMP0001",
        "punched_at": ms_exact
    })
    assert resp_dup.status_code == 409

    # 2. 09:40:01 IST on 2026-08-02 -> late = 10
    dt_late_10 = datetime(2026, 8, 2, 4, 10, 1, tzinfo=timezone.utc)
    ms_late_10 = int(dt_late_10.timestamp() * 1000)
    resp2 = test_client.post("/attendance/punch-in", json={
        "emp_code": "EMP0001",
        "punched_at": ms_late_10
    })
    assert resp2.status_code == 201
    assert resp2.json()["late_minutes"] == 10

    # 3. 10:15:59 IST on 2026-08-03 -> late = 45
    dt_late_45 = datetime(2026, 8, 3, 4, 45, 59, tzinfo=timezone.utc)
    ms_late_45 = int(dt_late_45.timestamp() * 1000)
    resp3 = test_client.post("/attendance/punch-in", json={
        "emp_code": "EMP0001",
        "punched_at": ms_late_45
    })
    assert resp3.status_code == 201
    assert resp3.json()["late_minutes"] == 45


def test_punch_out_and_rules_r3_r4_r5():
    # Punch in 09:28 IST (03:58 UTC) on 2026-08-10
    # Punch out 18:35 IST (13:05 UTC) -> 9.12 hours, overtime 5 min (< 30) -> 0, half_day False
    db.attendance_logs.delete_many({"emp_code": "EMP0001", "date": "2026-08-10"})

    in_utc = datetime(2026, 8, 10, 3, 58, 0, tzinfo=timezone.utc)
    test_client.post("/attendance/punch-in", json={
        "emp_code": "EMP0001",
        "punched_at": int(in_utc.timestamp() * 1000)
    })

    out_utc = datetime(2026, 8, 10, 13, 5, 0, tzinfo=timezone.utc)
    resp = test_client.post("/attendance/punch-out", json={
        "emp_code": "EMP0001",
        "punched_at": int(out_utc.timestamp() * 1000)
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["work_hours"] == 9.12
    assert data["overtime_minutes"] == 0
    assert data["half_day"] is False

    # Second punch out -> 409
    resp_dup = test_client.post("/attendance/punch-out", json={
        "emp_code": "EMP0001",
        "punched_at": int(out_utc.timestamp() * 1000)
    })
    assert resp_dup.status_code == 409

    # Half day test: work hours < 4.50
    db.attendance_logs.delete_many({"emp_code": "EMP0001", "date": "2026-08-11"})
    in_utc2 = datetime(2026, 8, 11, 4, 0, 0, tzinfo=timezone.utc)
    test_client.post("/attendance/punch-in", json={
        "emp_code": "EMP0001",
        "punched_at": int(in_utc2.timestamp() * 1000)
    })
    # Punch out after 4 hours (08:00 UTC)
    out_utc2 = datetime(2026, 8, 11, 8, 0, 0, tzinfo=timezone.utc)
    resp_half = test_client.post("/attendance/punch-out", json={
        "emp_code": "EMP0001",
        "punched_at": int(out_utc2.timestamp() * 1000)
    })
    assert resp_half.status_code == 200
    assert resp_half.json()["work_hours"] == 4.0
    assert resp_half.json()["half_day"] is True


def test_overnight_shift_r1_and_overtime():
    # EMP0005: shift 22:00 - 06:00 (overnight)
    # 22:00 IST is 16:30 UTC
    # 06:00 IST next day is 00:30 UTC next day
    db.attendance_logs.delete_many({"emp_code": "EMP0005", "date": "2026-08-15"})

    # Punch in 21:55 IST (16:25 UTC) on 2026-08-15
    in_utc = datetime(2026, 8, 15, 16, 25, 0, tzinfo=timezone.utc)
    resp_in = test_client.post("/attendance/punch-in", json={
        "emp_code": "EMP0005",
        "punched_at": int(in_utc.timestamp() * 1000)
    })
    assert resp_in.status_code == 201
    assert resp_in.json()["date"] == "2026-08-15"

    # Punch out 06:40 IST next day (01:10 UTC on 2026-08-16)
    # Shift end is 06:00 IST on 2026-08-16. 40 minutes overtime >= 30 -> overtime = 40
    out_utc = datetime(2026, 8, 16, 1, 10, 0, tzinfo=timezone.utc)
    resp_out = test_client.post("/attendance/punch-out", json={
        "emp_code": "EMP0005",
        "punched_at": int(out_utc.timestamp() * 1000)
    })
    assert resp_out.status_code == 200
    assert resp_out.json()["overtime_minutes"] == 40
    assert resp_out.json()["work_hours"] == 8.75


def test_regularize_and_audit_history():
    # Clean & insert a known record
    db.attendance_logs.delete_many({"emp_code": "EMP0001", "date": "2026-08-20"})
    in_utc = datetime(2026, 8, 20, 4, 35, 0, tzinfo=timezone.utc)  # 10:05 IST (35 min late)
    test_client.post("/attendance/punch-in", json={
        "emp_code": "EMP0001",
        "punched_at": int(in_utc.timestamp() * 1000)
    })

    # Correct punch-in to 09:28 IST (03:58 UTC)
    corrected_in_utc = datetime(2026, 8, 20, 3, 58, 0, tzinfo=timezone.utc)
    patch_resp = test_client.patch(
        "/attendance/EMP0001/2026-08-20",
        json={
            "punch_in": int(corrected_in_utc.timestamp() * 1000),
            "reason": "biometric glitch",
            "regularized_by": "hr.admin"
        }
    )
    assert patch_resp.status_code == 200
    data = patch_resp.json()
    assert data["late_minutes"] == 0
    assert len(data["history"]) == 1
    hist = data["history"][0]
    assert hist["by"] == "hr.admin"
    assert hist["reason"] == "biometric glitch"
    assert "late_minutes" in hist["changes"]
    assert hist["changes"]["late_minutes"] == {"from": 35, "to": 0}

    # Request that changes nothing -> 422
    patch_noop = test_client.patch(
        "/attendance/EMP0001/2026-08-20",
        json={
            "punch_in": int(corrected_in_utc.timestamp() * 1000),
            "reason": "again no change",
            "regularized_by": "hr.admin"
        }
    )
    assert patch_noop.status_code == 422


def test_analytics_and_explains():
    # 1. Employee monthly
    resp_emp = test_client.get("/analytics/employees/EMP0001/monthly?month=2026-07")
    assert resp_emp.status_code == 200
    emp_data = resp_emp.json()
    assert "working_days" in emp_data
    assert "present_days" in emp_data

    # 2. Department summary
    resp_dept = test_client.get("/analytics/departments/summary?month=2026-07")
    assert resp_dept.status_code == 200
    dept_data = resp_dept.json()
    assert len(dept_data["items"]) >= 1

    # 3. Leaderboard late
    resp_lb = test_client.get("/analytics/leaderboard/late?month=2026-07&limit=10")
    assert resp_lb.status_code == 200
    lb_data = resp_lb.json()
    assert "items" in lb_data

    # 4. Department trend
    resp_trend = test_client.get("/analytics/departments/Engineering/trend?from=2026-07-01&to=2026-07-10")
    assert resp_trend.status_code == 200
    trend_data = resp_trend.json()
    assert len(trend_data["items"]) == 10

    # 5. Admin explain checks
    for ep in ["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"]:
        url = f"/admin/explain/{ep}?emp_code=EMP0001&month=2026-07&department=Engineering&from=2026-07-01&to=2026-07-10"
        resp_exp = test_client.get(url)
        assert resp_exp.status_code == 200, f"Explain failed for {ep}: {resp_exp.text}"
        data_exp = resp_exp.json()
        assert data_exp["endpoint"] == ep
        assert "explain" in data_exp
