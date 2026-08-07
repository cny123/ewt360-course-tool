# Release Verification Record

## Release artifact

- Entry point: `ewt360_final.py`
- Historical baseline: `ewt_auto.py.orig` (local only, excluded from release)
- No account, password, token, or live-service response is stored here.

## Verified commands

Python executable:

`C:\Users\cny\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe`

1. `& 'C:\Users\cny\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m py_compile ewt360_final.py`

   Exit status: `0`.

2. `& 'C:\Users\cny\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' verify_fast_offline.py`

   Output: `release offline verification: PASS`. Exit status: `0`.

3. `& 'C:\Users\cny\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' ewt360_final.py --help`

   Help output displayed. Exit status: `0`.

The offline tests cover the oversized-heartbeat guard and verified completion.
No network request was made during verification.
