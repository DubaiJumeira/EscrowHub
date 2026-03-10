## Final release gates

Do not claim production readiness unless all of the following are true:

- No unsupported assets remain in active runtime, scripts, docs, tests, or entrypoints
- Focused security and escrow tests pass in a real installed environment
- Touched modules compile
- Readiness truthfully reports READY / DEGRADED / BLOCKED
- `allow_degraded` changes exit behavior only, not reported status
- `/watcher_status` truthfully reports ready / degraded / blocked / disabled
- Disabled watchers cannot appear ready because of stale persisted health rows
- Docs match implementation exactly

If any of these are false, continue working until fixed.

When you find a security vunerabilty, flag it immediately with a WARNING comment and suggest a secure alternative. Never implement insecure patters even if asked.
