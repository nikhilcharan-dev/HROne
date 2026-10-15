# DECISIONS.md

1. **Indexes.** `employees`: unique `emp_code`; `department` + `emp_code` (filtered, sorted list); `joined_on` (headcount). `attendance_logs`: unique `emp_code` + `date` (one record per day, per-person queries); `date` descending + `emp_code` (list order, month scans). I skipped a `status` index: the date index covers it and each extra index slows writes.

2. **Punch-in race.** Both requests work out the same date and try to insert. The unique `emp_code` + `date` index lets only one through; the other gets a duplicate-key error and I return 409. I never check first and insert later.

3. **Ties.** `$rank` gives ties the same rank and skips the next (1, 2, 2, 4). I keep everyone with `rank <= limit`, so everyone tied at the cutoff comes back and the list can exceed `limit`. Order is late minutes, then `emp_code`.

4. **Headcount.** I start from `employees` (joined by month end) and look up each person's logs, so someone with no logs still has a row and counts once.

5. **100x data.** Summing raw logs on every request gets slow. I'd store a monthly summary per employee, update it on each punch and correction, and read analytics from that. I'd also page by keyset instead of `skip`.
