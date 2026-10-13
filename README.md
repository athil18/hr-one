# Employee Attendance & Analytics API

A FastAPI and MongoDB service for employee attendance tracking (punch-in/punch-out with business rules R1–R5), regularization with audit trails, and MongoDB aggregation analytics (monthly summaries, department metrics, leaderboard with standard competition ranking, and daily trends with gap-filling).

## Setup & Running

1. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

2. **Environment Variables:**
   Provide `MONGO_URI` and `MONGO_DB` via environment variables or a local `.env` file (see `.env.example`):
   ```bash
   MONGO_URI=mongodb://localhost:27017
   MONGO_DB=attendance_db
   ```

3. **Start the service:**
   ```bash
   uvicorn app.main:app --port 8000
   ```
   All database indexes are created idempotently on startup.

4. **Seed Sample Data (Optional):**
   ```bash
   python sample_seed.py
   ```

5. **Run Tests:**
   ```bash
   pytest -v
   ```

## Implementation Highlights
- **Part A (`REVIEW.md`):** Complete code review identifying 21 defects in the starter codebase with failing examples, root causes, and fixes.
- **Part B (`app/main.py`):** 100% implemented according to `openapi.yaml` and business rules R1–R10 (timezones in IST, overnight shifts, half-up rounding, race-safe punch-in/out, optimistic locking for regularization, and aggregations for monthly, department summary, leaderboard, and trend).
- **Part C (`DECISIONS.md`):** Explanations for indexing strategies, concurrency handling, tie handling, headcount tracking with zero logs, and scaling architectures.
- **Explain Endpoint (`/admin/explain/{endpoint}`):** Provides raw MongoDB explain documents with `executionStats` confirming index scans (`IXSCAN`) and zero collection scans (`COLLSCAN`).
