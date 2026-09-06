# server/AGENTS.md — scoped rules for the server

The repository-wide rules in the root `AGENTS.md` apply here in full. These add to them,
and are loaded only when an agent is working in this directory.

## aiohttp: never reassign `AppKey` values after app startup

`app[some_key] = new_value` in a request handler or background task triggers
`DeprecationWarning: Changing state of started or joined application is deprecated`. Store a
mutable container under the key at creation and mutate it:

```python
# At app creation (fine):
app[my_key] = {"value": None, "flag": False}
# In a handler (fine — mutating the dict, not the key):
app[my_key]["value"] = time.monotonic()
# BAD — reassigning the key after startup:
app[my_key] = time.monotonic()
```

## Stateful features: initialise timestamps at startup, never lazily

If a watchdog or timeout compares `time.monotonic() - last_seen`, initialise `last_seen` to
`time.monotonic()` in the app startup hook, not to `None` — an `if last is not None` guard never
fires on a fresh server that has never received data.

The same reasoning applies to a newly connected client: `handle_ws` must reflect the *current*
server state immediately, so if the feed is already known lost or timed out it sends `no_feed`
right after the initial `full` message. Otherwise the client shows stale persisted data until the
next watchdog poll.
