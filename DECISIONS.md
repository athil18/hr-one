# DECISIONS.md  (about 150-200 words in total - plain language, your own words)

1. **Indexes.** We created unique indexes on `employees(emp_code)` (prevents duplicate creates, powers O(1) lookups) and `attendance_logs(emp_code, date)` (guarantees one record per employee-day, race-safe punch-ins). We indexed `attendance_logs(date, emp_code)` for sorted pagination without in-memory sorts, `employees(department, joined_on)` for department analytics, and `attendance_logs(date, late_minutes)` for leaderboard filtering. We considered indexing `history.at` but rejected it because history is an append-only audit trail never queried on its own.

2. **Punch-in race.** Both requests compute the attendance date and attempt insertion. MongoDB's unique index on `(emp_code, date)` enforces atomicity at the storage engine level: exactly one insert commits (returning 201 Created), while the concurrent insert raises `DuplicateKeyError`, which our application catches to return 409 Conflict.

3. **Ties.** We use MongoDB's `$rank` window function for standard competition ranking (1, 2, 2, 4). Filtering `rank <= limit` occurs after ranking, so all employees tied at the cutoff are included, even if total returned rows exceed `limit`. Tied rows are ordered by `emp_code` ascending.

4. **Headcount.** Our aggregation starts from `employees` (matching `joined_on <= month_end`), performing a `$lookup` into `attendance_logs`. Headcount is computed as `$sum: 1` over matched employees, ensuring employees with zero logs are fully counted.

5. **One thing you would change** if this had to serve 100x the data: We would maintain pre-aggregated daily department rollups updated at punch time, enabling O(1) analytics without scanning millions of raw punch documents.
