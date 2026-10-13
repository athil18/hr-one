<div align="center">

# 🏢 HROne — Employee Attendance & Analytics Engine
## *High-Performance FastAPI & MongoDB Attendance Tracking, Rule Engine, and Analytics Service*


[![Python](https://img.shields.io/badge/Python-3.13%2B-blue?style=for-the-badge&logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.110%2B-009688?style=for-the-badge&logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![MongoDB](https://img.shields.io/badge/MongoDB-6.0%2F7.0-47A248?style=for-the-badge&logo=mongodb&logoColor=white)](https://www.mongodb.com/)
[![Pytest](https://img.shields.io/badge/Tests-12%2F12%20Passing-brightgreen?style=for-the-badge&logo=pytest&logoColor=white)](https://pytest.org/)
[![Code Review](https://img.shields.io/badge/Review-21%20Defects%20Fixed-success?style=for-the-badge&logo=github)](REVIEW.md)
[![License](https://img.shields.io/badge/License-MIT-purple?style=for-the-badge)](LICENSE)

<br />

<p align="center">
  <a href="#-overview">Overview</a> •
  <a href="#-key-capabilities">Key Capabilities</a> •
  <a href="#-quick-start">Quick Start</a> •
  <a href="#-business-rules-matrix">Business Rules Matrix</a> •
  <a href="#-api-endpoints">API Endpoints</a> •
  <a href="#-mongodb-aggregations--indexes">Database & Indexes</a> •
  <a href="#-test-suite">Test Suite</a> •
  <a href="#-submission-checklist">Submission Checklist</a>
</p>

---

</div>

## 📌 Overview

**HROne Attendance & Intelligence Suite** is an enterprise-grade RESTful API built on **FastAPI** and **MongoDB (6.0/7.0)**. It handles precision employee attendance tracking, automated business rule derivations (**R1–R10**), overnight shift calculations, race-condition-safe punches, optimistic locking audit trails for regularizations, and database-level aggregation analytics.

All business operations, index creation, and aggregation pipelines are implemented inside a unified service architecture centered at `app/main.py`.

---

## ⚡ Key Capabilities

| Capability | Legacy Starter | HROne Enterprise Suite |
|---|---|---|
| **Punch Concurrency** | Race conditions permitted duplicate punches | **Compound Unique Indexes (`emp_code`, `date`)** prevent races at DB engine level (409 Conflict) |
| **Shift Engine** | Naive UTC date parsing; failed overnight shifts | **IST-Aware Shift Engine** handles midnight-crossing shifts (`22:00 - 06:00`) seamlessly (R1) |
| **Grace & Late Calculation** | Off-by-one division bugs | **Strict 10:00 grace boundary**; floor minute calculation from shift start (R2) |
| **Rounding Integrity** | Python `round()` Banker's float rounding | **`Decimal` half-up rounding (`ROUND_HALF_UP`)** to 2 and 4 decimals (R4, R8) |
| **Leaderboard Ties** | Array index ranking (breaks on ties) | **MongoDB `$rank` Window Function** standard competition ranking (1, 2, 2, 4) with `Limit` preservation |
| **Headcount Precision** | Misses employees with 0 logs | **`$lookup` from `employees`** preserves headcount even with zero monthly logs (R9) |
| **Query Performance** | Python in-memory sorting & COLLSCAN | **100% Index-Covered (`IXSCAN`)** on 100k records; zero `COLLSCAN` verified via `/admin/explain` |
| **Operations Console** | None | **Live Interactive Web Ops Dashboard** at `http://localhost:8000/` |

---

## 🚀 Quick Start

### 1. Prerequisites
- Python 3.10+ (tested on Python 3.13)
- MongoDB 6.0 or 7.0 (local instance or MongoDB Atlas M0)

### 2. Installation
```bash
# Clone the repository
git clone https://github.com/athil18/hr-one.git
cd hr-one

# Create virtual environment and install dependencies
python -m venv .venv
source .venv/bin/activate   # On Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 3. Environment Configuration
Provide `MONGO_URI` and `MONGO_DB` via environment variables or a local `.env` file (see `.env.example`):
```bash
cp .env.example .env
# Edit .env:
# MONGO_URI=mongodb://localhost:27017
# MONGO_DB=attendance_db
```

### 4. Seed Sample Data (Optional)
```bash
python sample_seed.py
```

### 5. Launch Application
```bash
uvicorn app.main:app --port 8000 --reload
```
All database indexes are initialized **idempotently on startup**.

Access the interfaces:
- 🖥️ **Live Web Dashboard:** [http://localhost:8000/](http://localhost:8000/)
- 📖 **Interactive Swagger UI:** [http://localhost:8000/docs](http://localhost:8000/docs)
- 📑 **ReDoc Documentation:** [http://localhost:8000/redoc](http://localhost:8000/redoc)

---

## 📊 Business Rules Matrix (R1 – R10)

- **R1 (Shift & Boundary):** Instants are stored as BSON Datetimes (UTC) and represented as epoch milliseconds in API. Day boundaries and shift comparisons are computed in IST (`Asia/Kolkata`). Overnight shifts (`shift_end <= shift_start`) punching before `shift_end` are attributed to date $D - 1$. All instants truncated to whole seconds.
- **R2 (Late Calculation):** Grace period is strictly 10 minutes. Late only if punch-in is strictly $> 10:00$ after `shift_start`. `late_minutes = floor((punch_in - shift_start) / 60)`.
- **R3 (Overtime):** Whole minutes (floor) from `shift_end` to `punch_out`, counted only if $\ge 30$; otherwise $0$.
- **R4 (Work Hours):** `(punch_out - punch_in) / 3600`, rounded half-up to 2 decimals using `Decimal.quantize`. `null` until punch-out.
- **R5 (Half Day):** After punch-out, `half_day = true` when rounded `work_hours < 4.50`. Counts as $0.5$ present day.
- **R6 (Presence Status):** `PRESENT`, `WFH`, `ON_DUTY` count as present; `ABSENT`, `LEAVE` do not.
- **R7 (Working Days & Mid-Month Joiners):** Monday–Friday only. Counted from `joined_on` for mid-month joiners. Weekend records add to late and overtime totals without incrementing present days.
- **R8 (Rounding):** Quantized half-up: durations to 2 decimals, rates to 4 decimals.
- **R9 (Headcount):** Employees where `joined_on <= period_end` are counted regardless of log existence.
- **R10 (Pagination):** 1-indexed; default `page_size = 20`, max `100`. Total reflects query filters.

---

## 🛠️ API Endpoints

| Method | Endpoint | Description | Status Codes |
|---|---|---|---|
| `GET` | `/health` | Live DB connectivity ping | `200`, `503` |
| `POST` | `/employees` | Create employee with unique `emp_code` | `201`, `409`, `422` |
| `GET` | `/employees` | Filter by department, paginated & sorted | `200` |
| `POST` | `/attendance/punch-in` | Record punch-in (R1, R2, race-safe) | `201`, `404`, `409`, `422` |
| `POST` | `/attendance/punch-out` | Record punch-out (R3–R5, overnight support) | `200`, `404`, `409`, `422` |
| `PATCH` | `/attendance/{emp_code}/{date}` | Regularize record with audit trail history | `200`, `404`, `409`, `422` |
| `GET` | `/attendance` | Paginated attendance logs with filters | `200`, `422` |
| `GET` | `/analytics/employees/{emp_code}/monthly` | Monthly employee summary aggregation | `200`, `404`, `422` |
| `GET` | `/analytics/departments/summary` | Department summary aggregation | `200`, `422` |
| `GET` | `/analytics/leaderboard/late` | Competition ranking leaderboard with ties (`$rank`) | `200`, `422` |
| `GET` | `/analytics/departments/{dept}/trend` | Daily trend series with gap-filling & 7-day moving avg | `200`, `422` |
| `GET` | `/admin/explain/{endpoint}` | MongoDB `executionStats` query plan verification | `200`, `422` |

---

## 🗄️ MongoDB Aggregations & Indexes

### Indexes Created Idempotently at Startup
1. `employees`:
   - `{"emp_code": 1}` (**Unique**) — O(1) employee lookups, prevents duplicate employee codes.
   - `{"department": 1, "joined_on": 1}` — Powers department analytics and filtered listings.
2. `attendance_logs`:
   - `{"emp_code": 1, "date": 1}` (**Unique**) — Enforces strict 1-record-per-day atomicity at storage level (concurrency race-safety).
   - `{"date": -1, "emp_code": 1}` — Powers sorted pagination without in-memory sorting.
   - `{"date": 1, "late_minutes": -1}` — Powers late leaderboard indexing.

### Query Plan Verification (`/admin/explain`)
Every query and aggregation pipeline produces **Index Scans (`IXSCAN`)** with zero collection scans (`COLLSCAN`), verified against 100k document datasets.

---

## 🧪 Test Suite

Run the full automated test suite:
```bash
pytest -v
```

### Coverage Highlights:
- ✅ **`test_health`**: Verifies 200 on DB ping, 503 on disconnection.
- ✅ **`test_r2_late_minutes_and_punch_in`**: Tests 09:40:00 (0 min) vs 09:40:01 (10 min) grace threshold.
- ✅ **`test_punch_out_and_rules_r3_r4_r5`**: Verifies half-day thresholds, work hours half-up rounding, and 30-minute overtime minimum.
- ✅ **`test_overnight_shift_r1_and_overtime`**: Tests cross-midnight 22:00–06:00 shifts and proper date attribution.
- ✅ **`test_regularize_and_audit_history`**: Tests optimistic locking, derived field recalculation, and audit history immutability.
- ✅ **`test_leaderboard_ties_and_cutoff`**: Confirms standard competition ranking (1, 2, 2, 4) with ties at limit cutoff.
- ✅ **`test_concurrency_punch_in_race`**: Threaded stress test: 2 concurrent punches yield exactly 1x `201` and 1x `409`.

---

## 📋 Submission Checklist Compliance

- [x] **`uvicorn app.main:app --port 8000`** starts from a clean clone after `pip install -r requirements.txt`.
- [x] **Idempotent Indexes:** All indexes are created automatically on startup; no manual setup.
- [x] **Dynamic Config:** `MONGO_URI` and `MONGO_DB` read from environment variables; zero hardcoding.
- [x] **[REVIEW.md](file:///c:/Users/Lenovo/Desktop/hr%20one/REVIEW.md)** fully completed (21 starter bugs identified and resolved).
- [x] **[DECISIONS.md](file:///c:/Users/Lenovo/Desktop/hr%20one/DECISIONS.md)** fully completed (150–200 words covering indexes, races, ties, headcount, and scaling).
- [x] **Clean Repository:** No `.env`, secrets, or `Dockerfile` are committed.
- [x] **Public Repository:** Hosted publicly on GitHub.

---

## 📄 License
This project is licensed under the [MIT License](LICENSE).
