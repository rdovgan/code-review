## Java pitfalls worth checking in changed code

- Transactions: a new write path outside `@Transactional`, self-invocation of a `@Transactional`/`@Async` method (the proxy is bypassed), checked exceptions that do not roll back, a transaction held open across a remote call.
- JPA/Hibernate: lazy collections read in a loop (N+1), `LazyInitializationException` after the session closed, entity mutated without save in a detached context, `equals`/`hashCode` on mutable or generated IDs.
- Equality and types: `==` on `String`/`Integer`/`Long` boxed values, `BigDecimal.equals` vs `compareTo`, `double` for money, integer overflow in `int` arithmetic on IDs/amounts/millis.
- Dates: `java.util.Date`/`Calendar` with the default time zone, `LocalDate` vs `ZonedDateTime` confusion for check-in/check-out style dates, DST in day counting.
- Collections and streams: modifying a collection while iterating it, `Collectors.toMap` without a merge function on non-unique keys, `Optional.get()` without a check on a value that can be empty in the visible flow, a stream result that is never used.
- Resources and concurrency: streams/connections opened without try-with-resources, shared mutable state in a singleton bean, non-thread-safe `SimpleDateFormat`/`HashMap` shared between threads.
- Security: string-concatenated SQL/HQL/JPQL with request data, `Runtime.exec` with user input, deserialization of untrusted data, secrets or personal data written to logs.
