## PHP pitfalls worth checking in changed code

- Comparison: `==` with type juggling (`"0" == false`, `"abc" == 0` in PHP 7), `in_array`/`array_search` without strict mode, `empty()` treating `"0"` as empty.
- Arrays: reading a key that the visible flow does not guarantee, `array_merge` renumbering numeric keys, references (`&$x`) in `foreach` reused after the loop.
- Database: queries inside loops (N+1), unchecked return value of a query or `execute`, a multi-step write without a transaction.
- Dates: `date()`/`strtotime()` with the server default time zone, DST in day counting.
- Security: request data (`$_GET`, `$_POST`, request objects) in SQL without prepared statements, unescaped output (XSS), `eval`/`system`/`exec`/`include` with user input, missing CSRF/auth checks on state-changing endpoints, secrets in code or logs.
