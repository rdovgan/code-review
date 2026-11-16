## JavaScript / TypeScript pitfalls worth checking in changed code

- Async: a missing `await` on a promise whose result or error matters, `forEach` with an async callback (not awaited), unhandled rejections, races between concurrent requests updating the same state.
- React: state updated from a stale closure, effects with missing or wrong dependencies causing loops or stale data, list keys that are indexes on reorderable lists, state mutated in place.
- Types and values: `==` coercion, `||` used for defaults where `0`/`""`/`false` are valid (use `??`), floating-point money arithmetic, `Date` parsing/time-zone assumptions, `any` hiding a wrong shape from an API.
- Data handling: API response fields read without the shape the backend actually returns, pagination or error responses ignored, event listeners/subscriptions/timers never cleaned up.
- Security: `innerHTML`/`dangerouslySetInnerHTML` with user data, `eval`/`new Function`, `postMessage` without origin check, secrets or tokens in client code or logs, URLs built from user input without validation.
