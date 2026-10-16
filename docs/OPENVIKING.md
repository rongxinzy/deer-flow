# OpenViking memory backend

DeerFlow can use a remote OpenViking server as an optional long-term memory
backend. DeerMem remains the default. The OpenViking backend uses the maintained
[`langchain-openviking`](https://pypi.org/project/langchain-openviking/)
package instead of implementing OpenViking's HTTP protocol inside DeerFlow.

## Current scope

The first official-adapter integration deliberately preserves DeerFlow's
existing automatic-memory behavior:

- memory is recalled through DeerFlow's existing fixed memory query;
- completed turns are captured by the existing memory middleware;
- messages about to be compacted are captured by the existing summarization hook;
- every accepted capture is committed to the thread's stable OpenViking Session;
- the official adapter handles message conversion, tool calls and results,
  100-message batching, partial-write progress, commit retry, and SDK transport;
- one recorder-owned SDK client is shared with retrieval and closed through
  DeerFlow's existing memory shutdown contract.

This backend supports `memory.mode: middleware`. Memory governance is served from
memory files: `get_memory` / `clear_memory` / `create_fact` / `update_fact` /
`delete_fact` are implemented, so the gateway's fact endpoints and the Settings
memory-document view work against OpenViking instead of answering 501.
Import/export and the cache-reload operation are not implemented. OpenViking MCP
tools are a separate integration surface and are not enabled by this backend.

## Authentication boundary

This version is for one DeerFlow user backed by one ordinary OpenViking **USER
API key**. OpenViking derives the account and user from that credential.
DeerFlow does not configure trusted account/user headers and must not receive a
root key for normal memory traffic.

The supported server configuration is OpenViking `api_key` mode, where the USER
key determines the account and user. DeerFlow supplies its URL and API key
explicitly, overrides any ambient actor peer during memory operations, and does
not inherit arbitrary HTTP headers from `ovcli.conf`.

Before enabling this backend, remove legacy `OPENVIKING_ACCOUNT` and
`OPENVIKING_USER` values from DeerFlow's repository-root `.env` and service
environment, and remove `account` and `user` defaults from
`~/.openviking/ovcli.conf`. Those settings belong to trusted-mode
configurations and are outside this adapter's supported setup.

`owner_user_id` binds the configured key to one DeerFlow identity. Use
`default` when DeerFlow authentication is disabled. In an authenticated
single-user deployment, use that user's DeerFlow ID. A request for another
DeerFlow user is rejected before OpenViking is contacted, preventing one USER
key from silently sharing memory across users.

Multi-user credential provisioning and storage are intentionally outside this
first adapter PR.

Existing trusted-mode configurations are not migrated automatically. Configure
the OpenViking server in `api_key` mode, replace `auth_mode`, `account`, and the
root key with `owner_user_id` and a USER key, and remove the legacy ambient
identity settings listed above. Because the credential-bound user and Session
mapping differ from the old trusted-user mapping, previously captured
trusted-mode data remains in its old OpenViking namespace rather than being
silently reassigned.

## Configure DeerFlow

Create or select an OpenViking user, then copy its USER API key into DeerFlow's
repository-root `.env`:

```dotenv
OPENVIKING_API_KEY=replace-with-an-openviking-user-api-key
```

Select the backend in `config.yaml`:

```yaml
memory:
  enabled: true
  injection_enabled: true
  shutdown_flush_timeout_seconds: 30
  manager_class: openviking
  mode: middleware
  backend_config:
    base_url: http://127.0.0.1:1933
    owner_user_id: default
    api_key_env: OPENVIKING_API_KEY
    startup_policy: fail_fast
    failure_policy:
      read: fail_open
      write: log_and_drop
    retrieval:
      top_k: 8
      score_threshold: 0.25
      max_injection_chars: 12000
      content_mode: overview
      drop_deleted_entries: true
      injection_query: >-
        user profile preferences important entities events ongoing goals
        constraints and prior decisions
```

For a host-installed OpenViking used by Docker DeerFlow, set `base_url` to
`http://host.docker.internal:1933` and `allow_insecure_http: true`. The optional
Compose overlay uses the internal `http://openviking:1933` address.

The dependency on `langchain-openviking==0.1.0` is declared by DeerFlow's
harness package and is installed by the normal `uv sync` flow.

## Start the services

For a local OpenViking process, start and verify the server first:

```bash
openviking-server doctor
openviking-server
curl http://127.0.0.1:1933/health
```

Then start DeerFlow normally:

```bash
make doctor
make dev
```

DeerFlow is available at <http://localhost:2026>. OpenViking Studio is
available at <http://localhost:1933/studio>.

To use the optional Docker service instead:

```bash
docker compose \
  -f docker/docker-compose.yaml \
  -f docker/docker-compose.openviking.yaml \
  up -d openviking

docker exec -it deer-flow-openviking openviking-server init

docker compose \
  -f docker/docker-compose.yaml \
  -f docker/docker-compose.openviking.yaml \
  up -d --build
```

Configure OpenViking in API-key mode and obtain a USER key through its identity
management flow. Only that USER key belongs in DeerFlow's
`OPENVIKING_API_KEY` variable.

## Identity and session mapping

One DeerFlow thread maps deterministically to one OpenViking Session. A commit
creates an archive inside that Session; it does not create a new Session, so a
thread keeps the same identity when the user returns later.

The default DeerFlow agent uses `default_peer_id` (`deerflow` by default).
Named agents use lowercase OpenViking peer IDs. Names that are not valid peer
IDs, conflict with the default, or enter the reserved `df-agent-` namespace are
mapped to collision-resistant IDs. USER-key identity remains the security
boundary; peers separate memory scopes within that user.

## Written facts

`create_fact` writes an ordinary OpenViking memory file under the actor's own
memory root — `viking://~/memories` for a whole-actor write, that actor peer's
`memories` directory for an agent- or requester-scoped one — so a fact created
through the gateway is recalled like any captured memory and appears in
`get_memory` without widening any scope. The file name is derived from the
normalized content (`str.strip().casefold()`, the same key `memory_add` compares
before writing), and the write is issued as an OpenViking *create*: the same fact
therefore always maps to the same URI, and a concurrent duplicate is rejected as
`ValueError("Duplicate fact")` (the gateway's 409, and what `memory_add` reports
as an error) instead of overwriting or duplicating the entry. That rejection is
only reported once the file at that URI is confirmed to hold the same content, so
an unreachable or failing backend still surfaces as its own error.

`update_fact` replaces the content of an existing entry, addressed by the URI
`search`/`get_memory` returned. An id outside the actor's scope is refused, and a
fact this actor does not currently list is reported as `KeyError` (the gateway's
404) rather than as an empty document. Both writes pass `wait=true`, because
OpenViking indexes new content in the background and recall would otherwise miss
a fact created moments earlier.

An OpenViking memory file holds content only. `category` and `confidence` are
accepted and validated for the DeerFlow contract but not persisted, and recall
reports the category OpenViking derives for an entry rather than DeerFlow's
label. No category directory is created for a fact: OpenViking's category
vocabulary does not cover DeerFlow's free-form labels, and its own extraction owns
the `memories/<type>/` directories. Directories inside a memory root are skipped
by the governance reads, because a directory is not a fact and reading one back as
a file fails.

## Deleted entries are not recalled

Retrieval reads OpenViking's index, and an index entry can outlive its file — a
delete that raced a pending index task, or an orphan that a rebuild keeps
(`reindex` rebuilds from the records it can see and reports
`deleted_records=0`). Such an entry still recalls the deleted content.

With `retrieval.drop_deleted_entries: true` (the default), recalled entries are
checked against the store before they reach the model, and entries whose file is
gone are dropped. The check costs one listing per directory a recall touches.
An entry is only dropped when it is definitively absent from a directory this
scope reads; a URI outside the scope roots, an entry without a URI, or a failed
listing keeps the entry, so a transient error never silently discards memory.
Set `drop_deleted_entries: false` to skip the check entirely.

## Retry and failure behavior

- `read: fail_open` logs retrieval failures and continues the turn without
  recalled OpenViking context. `read: raise` aborts the turn when recall fails.
  The same policy applies at DeerFlow's 5-second async injection deadline,
  even when the worker pool is full. If configuration or backend discovery
  has not finished by then, the unknown policy fails closed. This deadline
  limits request waiting, not the blocking operation: an in-flight read keeps
  its worker until the backend returns or reaches its own timeout
  (`timeout_seconds`, 30 seconds by default).
- `write: log_and_drop` logs capture failures without failing an already
  generated answer. `write: raise` propagates them.
- DeerFlow stores only hashes and counters in a bounded local capture cursor
  under `{storage_path}/openviking/sessions/`. It never stores message text
  there.
- The cursor prevents full LangGraph transcript snapshots from being submitted
  again. It also records confirmed progress from partial batches and retries a
  failed commit before appending more messages.
- An unreadable cursor fails closed because replaying an unknown prefix could
  duplicate private conversation history.
- Graceful shutdown stops new memory work, waits up to
  `shutdown_flush_timeout_seconds` for accepted operations, and closes the
  recorder-owned SDK client. It does not introduce a new DeerFlow lifecycle or
  background worker.

For deployments where a lost memory update is unacceptable, a durable outbox
is still required. This initial integration does not claim at-least-once
delivery.
