# Employee Attendance & Analytics API

FastAPI + MongoDB. All code is in `app/main.py`.

## Run

```bash
python -m venv .venv && source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -r requirements.txt
export MONGO_URI="mongodb://localhost:27017"           # required in practice
export MONGO_DB="attendance_db"
uvicorn app.main:app --port 8000
```

`MONGO_URI` and `MONGO_DB` are read from the environment (a local `.env` is also loaded, but real environment variables win). All indexes are created at startup and are idempotent. Needs Python 3.11+ and MongoDB 6.0+ (`$densify`, `$setWindowFields`).

## Status

Everything in the contract is implemented: employees, punch-in / punch-out, PATCH regularization with history, the four analytics endpoints (aggregation pipelines), and `/admin/explain/{endpoint}`. Nothing is missing.

I tested against MongoDB 7 in a throwaway container with a generated dataset of about 91,000 attendance records (ties, weekend records, mid-month joiners, employees with no logs, overnight shifts, legacy documents). Analytics were compared with an independent brute-force implementation, and I checked the explain plans for `IXSCAN` with no `COLLSCAN`.

## Choices where the contract left room

- **Trend range limit**: the 92-day limit counts rows, so `from`..`to` may span 92 calendar days inclusive; 93 is a 422.
- **`moving_avg_7d`**: the mean of the reported (4-decimal) `attendance_rate` values in the window.
- **Regularization conflicts**: the write is conditional on the record being unchanged since it was read (same status, punch times and history length). If another correction got in first, the loser gets 409.
- **"Changes nothing"**: decided on all seven tracked fields, derived ones included. The same diff is what goes into `history`.
- **Rounding**: half-up everywhere, using `Decimal` with `ROUND_HALF_UP` (Python's `round()` is banker's rounding).
- Synchronous PyMongo with plain `def` endpoints; FastAPI runs them in its thread pool.
