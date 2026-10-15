# DECISIONS.md

1. **Indexes.** `employees`: unique `emp_code`; `(department, emp_code)` for the filtered, sorted list and department pipelines; `joined_on` for headcount without a department. `attendance_logs`: unique `(emp_code, date)` for one record per day, race-safe inserts and per-employee scans; `(date desc, emp_code)` for the list sort and month-wide scans. Rejected: `(status, date, emp_code)`, since status filters can use the date index and it wasn't worth the extra writes.

2. **Punch-in race.** Both requests compute the same date and call `insert_one`. The unique `(emp_code, date)` index lets one succeed; the other raises `DuplicateKeyError`, returned as 409. No read-then-write gap.

3. **Ties.** `$rank` gives 1, 2, 2, 4. I keep `rank <= limit` after ranking, so everyone tied at the cutoff is returned and the list can exceed `limit`. Ordered by minutes, then `emp_code`.

4. **Headcount.** The pipeline starts from `employees` (joined on or before month end) and `$lookup`s their logs, so someone with no logs still counts once.

5. **100x.** Summing a month of raw logs per request won't scale. I'd keep a monthly rollup per employee, updated on punch and correction, and switch list paging from `skip` to keyset.
