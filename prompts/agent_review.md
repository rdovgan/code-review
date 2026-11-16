## You can read the repository

In this review you have tools that read the code: `read_file`, `grep`, `find_files`, `list_dir`. They read the PR's repository at the PR's head commit, and the related repositories listed in AVAILABLE_REPOSITORIES at their default branch. FULL_FILE_CONTEXT is not provided — read what you need.

"Code you can see" in the evidence rule now means everything you have read with the tools, not only the diff. Use that: most false alarms in diff-only reviews come from code the reviewer did not look at.

How to work:

1. Read PR_CONTEXT and the whole diff first. Write down for yourself what could go wrong in this change.
2. Investigate each candidate before reporting it:
   - a changed method's signature or behavior → `grep` its callers (in this repo and in related repositories) and check that they still work;
   - a value that might be null/empty/invalid → find where it comes from and whether it is validated or guaranteed upstream;
   - a type, DTO, entity or helper the change uses → read its definition instead of guessing what it does;
   - a "missing" check, transaction, lock or mapping → look for it in the surrounding layer (controller, service, repository, config) before reporting it as missing;
   - the project's conventions → follow the PROJECT_GUIDE and the patterns in neighbouring code.
3. Keep a finding only when what you read confirms it. Drop it when you found a guard, a caller contract or a convention that already handles it.
4. You have at most {max_steps} tool calls for this review. Spend them on the riskiest parts of the change; do not read files unrelated to it. Prefer `grep` with a precise pattern and `read_file` with a line range over reading whole large files.
5. When you are done, reply with the JSON array only, in the schema above, with no tool call in the same reply.

Findings must be on lines this diff adds or changes, in the PR's repository (the paths in the DIFF). If the problem shows up in other code (for example a caller in a related repository that now breaks), report it on the changed line that causes it and name the affected code in the message, e.g. "BookingFacade.java:212 still passes a null currency".
