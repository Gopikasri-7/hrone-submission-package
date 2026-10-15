# Architectural Decisions

### 1. Indexes
I established a unique index on `employees.emp_code` to guarantee core business key uniqueness. For `attendance_logs`, I created a compound unique index on `(emp_code, date)`. This accelerates daily summary queries and enforces the restriction of having at most one log per employee per day. I considered an index on `status` but rejected it because it has low cardinality and wouldn't help query performance.

### 2. Punch-in Race Condition
If two identical punch-in requests hit at the same millisecond, the first thread executing `find_one` will find no record and issue an `insert_one`. The competing thread might pass the `find_one` check simultaneously, but its subsequent insert will trigger a duplicate key exception from MongoDB because of our compound unique index on `(emp_code, date)`. The app handles this safely without corrupting data.

### 3. Leaderboard Ties
If two employees are tied on late minutes at the page limit boundary, the aggregation pipeline returns them based on MongoDB's internal retrieval sequence because no secondary sorting key was applied. To make a leaderboard stable at scale, I would add a secondary deterministic sort layer like `emp_code: 1`.

### 4. Zero Log Headcounts
The analytics pipeline uses a `$lookup` starting from the `attendance_logs` collection, which filters by month before matching profile data. To accurately count employees with zero logs, the operation should ideally run as a `$lookup` originating from the master `employees` collection down to the logs, ensuring inactive profiles are never left out of the monthly department headcount sums.

### 5. Scaling 100x Data Volume
To scale the architecture for 100x data, I would change the analytics summary from a runtime database aggregation query to a pre-computed cache layer. I would use MongoDB change streams or a cron job to update a dedicated daily metrics collection overnight, preventing heavy analytical lookups from slowing down the live punch endpoints.
