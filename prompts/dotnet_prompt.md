## C# / .NET pitfalls worth checking in changed code

- Async: `.Result`/`.Wait()` on a task (deadlocks, thread starvation), `async void` outside event handlers, a `CancellationToken` accepted but not passed on, fire-and-forget tasks whose exceptions are lost.
- EF Core: queries inside loops (N+1), `ToList()` before filtering (whole table loaded), tracked entities modified across `DbContext` lifetimes, `SaveChanges` missing or called per item in a loop.
- LINQ: multiple enumeration of an `IEnumerable` backed by a query or generator, `First()` on a sequence that can be empty in the visible flow.
- Types: `decimal` vs `double` for money, `DateTime.Now` vs `UtcNow` and `DateTimeKind` mixing, integer overflow in unchecked arithmetic.
- Resources: `IDisposable` (streams, `HttpClient` per call, DB connections) not disposed with `using`.
- Security: string-built SQL with request data, `Process.Start` with user input, XML parsing with DTD enabled, path traversal through user-supplied file names, secrets or personal data in logs.
