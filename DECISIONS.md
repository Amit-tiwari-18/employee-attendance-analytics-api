# DECISIONS.md

## 1. Indexes

I created a unique index on `emp_code` to prevent duplicate employees, and a compound unique index on `emp_code` and `date` to prevent duplicate daily attendance records. I also added indexes for department and joining date, attendance date and employee code, employee/date/status, and date/late minutes/employee code to support filtering and analytics. I considered adding more indexes for every possible filter combination, but rejected that approach because extra indexes increase storage and write overhead.

## 2. Punch-in Race

If two identical punch-in requests arrive simultaneously, both may pass the initial duplicate check. However, MongoDB's unique compound index allows only one record for the same employee and date. The other insert fails with a duplicate-key error, which the API handles by returning HTTP 409 Conflict.

## 3. Ties

The leaderboard uses standard competition ranking and sorts by late minutes, with employee code as a deterministic secondary sort. Ties at the requested limit are included, so the result may contain more employees than the limit.

## 4. Headcount

The department summary starts from employee records and looks up attendance logs. Therefore, employees with no attendance records are still included in the department headcount.

## 5. Scaling

For 100 times more data, I would review query execution plans and benchmark aggregation pipelines, then optimize indexes and consider caching frequently requested analytics.