## Python pitfalls worth checking in changed code

- Values: mutable default arguments, `is` used for value comparison, truthiness checks where `0`/`""`/`[]` are valid values, float arithmetic for money, naive vs aware `datetime` mixing.
- Errors: bare `except:` or `except Exception` that swallows a failure the caller needs to see, exceptions in a loop that leave state half-updated.
- Database/ORM: queries inside loops (N+1, missing `select_related`/`prefetch_related`), whole querysets loaded to check existence or count, multi-step writes without a transaction.
- Async: blocking calls (`requests`, `time.sleep`, sync DB drivers) inside `async` code, coroutines not awaited.
- Resources: files, sockets or connections opened without `with`.
- Security: SQL built with f-strings/format from request data, `subprocess`/`os.system` with user input (`shell=True`), `pickle`/`yaml.load` on untrusted data, path traversal, SSRF through user-supplied URLs, secrets or personal data in logs.
