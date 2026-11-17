# Spot Collector

Spot Collector merges DX-cluster and local-skimmer Telnet feeds for N1MM and
other logging clients. It retains the existing primary connection for logger
commands and manual DX spots, accepts GoCluster as a receive-only processed
feed, and forwards useful local single-receiver observations without waiting
for corroboration from another receiver.

## Operation

```text
Existing primary cluster <--> Collector <--> Logging clients
GoCluster processed feed   -->    ^
Local band skimmers        -->    |
```

All logging clients share the same upstream sessions and resulting feed. A
command sent to the primary can therefore change the primary session for every
client. GoCluster has a separate login/profile and does not receive forwarded
logger commands. Configure its desired CW/band/geographic selection directly;
GoCluster filters do not filter the other incoming feeds.

The source roles are `cluster`, `gocluster`, `local`, and `rbn`. Explicit roles
separate processed cluster output, direct local skimmers, and raw multi-receiver
feeds such as public RBN. Server position still
identifies the command primary; it no longer decides whether a feed is put
through the old RBN aggregator.

### Spot admission and repeat suppression

- A first unrelated local discovery is delivered promptly, including while
  frequency adjustment is unavailable. Suspicious callsign near-matches may
  briefly wait under the local quality policy below. A skimmer on another band
  cannot corroborate the same signal.
- Matching live spots from all sources share repeat suppression. The default
  window is 180 seconds. Ordinary repeats do not indefinitely renew that window.
- Up to two corroborated frequency-change updates can pass during an entry's
  lifetime. A change must exceed the configured movement tolerance and acquire
  two distinct receivers within the candidate window, including a local move.
  Repeats or mirrored copies of one receiver count once. Two suffice without a
  majority; a pending proposal expires without forced release or update-budget
  consumption. These are corroborated reports, not verified transmitter QSYs.
- A later GoCluster report does not bypass suppression solely to enrich a
  previously delivered spot. Evidence is observed before output admission, so
  suppressed reports can still inform eligible internal decisions.
- Terminal wire times may be followed by a valid 4-, 6- or 8-character
  Maidenhead grid. Undated reports cannot move an identity away from retained
  dated frequency evidence; a first undated report can still pass unchanged.
- Non-spot traffic remains relay traffic. Primary history requests receive a bounded bypass of 200 spot rows or five
  seconds. In plain text, concurrent live rows cannot always be distinguished
  from history rows; a few may pass during that bypass. Non-spot replies remain
  unchanged. Verify the actual primary's history format in staging.

Collector preserves the received spot suffix byte-for-byte. Only an eligible
frequency or DX-call header replacement changes the spot. Mode/report text,
spaces, comments, upstream tags, grids, confidence and time remain those of the
selected original line. No Collector Q/Z, calibration or warm-up labels are
added. Diagnostics belong to Collector status/logging, not spot comments.

### Corroboration and per-receiver frequency adjustment

The first spot remains immediate. A new frequency more than the configured
movement tolerance from the delivered anchor becomes pending. The default
candidate window is 10 seconds and an agreeing pair/group must fit within a
100-Hz spread. A second distinct receiver confirms the proposal; a repeated
receiver, another route carrying it, or a high upstream Q label does not.

Evidence is observed before repeat suppression. Promotion changes the anchor
and consumes one movement update. Previously collected anchor support cannot
be recycled automatically to confirm a return move. If incompatible groups
each reach two receivers, corroborated GoCluster-supported evidence has priority.
Equally qualified competing GoCluster groups leave the current anchor in place.
A lone GoCluster outlier cannot overrule two agreeing receivers.

Automatic adjustment is per receiver, band and input representation/route,
including remote skimmers. Different receivers on one public RBN connection do
not share an oscillator correction, and the raw and GoCluster-processed versions
of a receiver are not assumed to need the same adjustment. Original measurements
remain separate from Collector-adjusted output. Only outgoing Collector fields
change; hardware settings and direct RBN publishing remain untouched.

A transferable adjustment requires a sustained pattern over distinct calls,
time and frequency coverage, against qualified references excluding the subject
receiver and its mirrors. One HA8LNN disagreement may produce a pending move,
but must not teach a permanent IZ3EAW adjustment. Unknown aggregate contributors
cannot be expanded into independent votes. If the references or estimation gates
are inadequate, use the original report and ordinary movement corroboration.

This is **relative alignment to qualified reference evidence**, not independently
measured RF calibration. GoCluster can already correct or average frequencies,
and its hidden population can include a local report returned through RBN.
Reference priority cannot prove independence or absolute accuracy. Known aliases
are normalized conservatively; do not collapse different SSIDs into positive
support without receiver evidence. Reconnect and expiry retire affected evidence.

The following defaults are conservative engineering policy, not measured TK0C
accuracy, proven convergence or scientifically established acceptance limits.
The approximate reported 200-Hz drift is not a validated bound.

| `spot_processing` key | Default | Meaning |
| --- | ---: | --- |
| `enabled` | `true` | Enable source-aware processing. |
| `repeat_seconds` | `180` | Ordinary repeat lifetime. |
| `match_tolerance_hz` | `500` | Frequency association tolerance. |
| `move_tolerance_hz` | `500` | Movement threshold; equality is not a move. |
| `max_changes` | `2` | Movement updates per retained repeat lifetime. |
| `corroboration_seconds` | `10` | Fresh movement-evidence/candidate lifetime. |
| `local_call_hold_seconds` | `3` | Maximum wait for a suspicious local callsign variant. |
| `local_call_tolerance_hz` | `200` | Local-call association tolerance after qualified frequency adjustment. |
| `corroboration_tolerance_hz` | `100` | Maximum spread for an agreeing receiver group. |
| `calibration_match_tolerance_hz` | `1000` | Separate maximum association radius for qualified calibration comparisons; not permission to treat arbitrary nearby calls as one event. |
| `max_age_seconds` | `300` | Maximum observation age. |
| `future_tolerance_seconds` | `120` | Permitted future-time tolerance. |
| `reference_ttl_seconds` | `120` | Reference freshness lifetime. |
| `pair_window_seconds` | `30` | Maximum time separation for paired evidence. |
| `calibration_window_seconds` | `1800` | Retained calibration evidence window. |
| `calibration_min_calls` | `5` | Distinct calls needed for adjustment. |
| `calibration_retry_batch` | `16` | Maximum older missing subjects retried when reference authority changes. |
| `calibration_min_span_seconds` | `30` | Minimum elapsed span across qualifying calls. |
| `calibration_min_span_hz` | `10000` | Minimum evidence frequency span. |
| `calibration_max_offset_hz` | `1000` | Adjustment safety limit. |
| `calibration_max_scatter_hz` | `30` | Residual-scatter limit before the uncertainty allowance. |
| `calibration_max_slope_ppm` | `5` | Scale-like disagreement safety limit after quantization allowance. |
| `calibration_uncertainty_hz` | `50` | Engineering reference-quantization allowance; not measured RF uncertainty. |
| `max_entries` | `4096` | Capacity of each retained dictionary. |

Retained entries own their lookup-index memberships. Expiry, eviction, source
reset and explicit clearing release those memberships together. Each store also
has an expiry heap capped at `2 * max_entries + 64` records; stale heap records
retain keys rather than spot payloads. Normal matching uses identity and nearby
frequency indexes instead of sweeping every retained spot for every input.


### Local-skimmer callsign quality

Only `local` inputs use local busted-call checking. Public `rbn`, ordinary
cluster and GoCluster lines keep their existing external processing policy.
Normalize calls to uppercase; a replacement must differ by exactly one inserted,
deleted or substituted character. Transpositions do not qualify. Reported and replacement calls must both have more than three characters. Portable calls containing a slash are excluded from automatic replacement in
this initial implementation; do not strip their prefix or suffix to force an
association.

A replacement requires two distinct external receiver identities, fresh CW
activity in the same band and a unique qualifying nearby call after qualified
local frequency adjustment. Independent nearby external support for the original
local call blocks replacement. GoCluster C is no longer required for this local
policy. A strong external call elsewhere or a familiar callsign is insufficient.

An unrelated local-only call passes promptly. A suspicious near-match with
insufficient corroboration can wait up to `local_call_hold_seconds` (default 3).
New evidence can settle it early. A timer checks deadlines even if no more spots
arrive. If unresolved, release the original call; do not convert timeout into
correction authority. Evidence received at or after the deadline cannot
authorize correction of that expired hold, even if the timer runs late. This
also applies to the evidence that established a frequency adjustment.
Rechecks do not refresh original observations, sample
ages or receiver votes. Held work remains age-limited; source reconnection
prevents an old hold borrowing new-epoch evidence. Hold capacity uses `max_entries`;
overflow releases without an extra wait. Shutdown resolves valid held work
before client retirement, without guaranteeing every queued spot is flushed.

Frequency and callsign choices remain separate. Exact original comments are
preserved for corrected and released local spots. Debug-verbose shows HOLD,
correction or release reasons, candidate and external support. Status reports
current holds and evaluated local corrections/releases (including outputs later
suppressed by ordinary repeat policy).

The initial `local_call_tolerance_hz` is 200 Hz. It is a conservative engineering
matching policy, not measured RF accuracy; qualified receiver adjustment is
considered first. Two valid nearby calls remain possible, so competing support
and ambiguity guards must be kept.

## Configure and run

Use Python 3.7+ and the standard library. No additional runtime packages are
required. JSON supports a variable number of servers; the legacy CLI supports
four numbered servers. Legacy entries without roles default to `cluster` for
the first server and `local` for later servers; use the JSON `servers` list to
classify additional processed clusters correctly. Inspect available options with:

```sh
python3 spot-collector.py --help
```

Start from this existing checkout after editing a copy of the example:

```sh
python3 spot-collector.py --config /path/to/spot-collector.json
```

When `--config` is supplied, JSON is authoritative and other CLI options are
ignored. The checked-in JSON is an **example requiring site configuration**;
`.invalid` names are deliberate placeholders, not production endpoints. Replace
`CALLSIGN-X` with the station's actual login identity before running it.

Each JSON server accepts `name`, `address`, `note`, `direction`, `role`, and
optional `enabled` (default true), `callsign`, `startup_commands` and `ready_text`.
Names must be unique. Disabled entries are still validated but are not dialed.
Use `role: "rbn"` with `direction: "in"` for raw multi-receiver feeds. A legacy
`local` entry pointing at the known host `telnet.reversebeacon.net` is normalized
to `rbn` with a startup warning, including the current station test configuration.
This changes only the in-memory source definition, not the user's JSON file.
Other public/proxied multi-receiver feeds need an explicit role; their identity
cannot be inferred reliably from a port number or arbitrary hostname.

GoCluster is configured and enabled at `cluster.n2wq.com:8300` with login
`TK0C`. The replacement fourth skimmer remains a disabled placeholder. Repeat local entries
for the remaining bands. Each setup command is limited to 512 characters. A per-server `callsign` is sent exactly as configured;
without it the legacy full-primary/base-secondary login convention remains.

GoCluster and raw RBN feeds must have `direction: "in"`. Prefer provisioning its saved profile
separately. If connection setup commands are needed, configure at most 16
single-line `startup_commands` and a `ready_text` literal matching a known
post-login greeting (at most 256 characters). They are sent after login prompt
recognition and that readiness line, not after an arbitrary timer. Do not invent
a greeting without inspecting the deployed GoCluster transcript. Session-only
commands must run again after reconnect; saved preferences remain profile-owned.

Direction `in` means upstream-to-clients, `out` means clients-to-upstream, and
`both` means both. Keep only the existing primary outbound-enabled for the
agreed command route. Give GoCluster an unused numeric-SSID callsign/profile;
reusing an active identity can replace another GoCluster session. Keep its
ordinary text output and diagnostics off for initial N1MM qualification; native
CC11 has a different wire format and is not parsed as a conventional spot.

Zero client/server inactivity timeout disables that application timeout; TCP
keepalive remains separate. Independent upstream tasks reconnect with bounded
backoff. Complete lines and Telnet negotiation are framed across socket reads.
Upstream framing also recognizes structured `CALL de NODE>` and
`CALL de NODE DD-Mon-YYYY HH:MM[:SS] UTC>` command prompts without a newline.
This keeps a following DX line separate; arbitrary spot-comment `>` characters
are not prompt boundaries. Telnet application IAC bytes are re-escaped on output
to preserve application comments and command bytes in both directions.

Client send queues are bounded; a lagging client is disconnected. A stalled graceful
close is followed by a transport abort and bounded close join, so sockets and
client handlers retire. A failed login write ends that connection and enters
the existing reconnect backoff instead of waiting indefinitely for input.
 The downstream
login prompt is a compatibility prompt, not client authentication. Run the
listener only in the intended trusted network.

Local commands are `status`, `longstatus`, `list`, `uptime`, and `connect`. They are handled
as complete lines; ordinary commands go to outbound-enabled upstreams. All
clients share upstream state.
Client Ctrl+C (raw ETX or Telnet Interrupt Process) disconnects that client
immediately without stopping Collector or its shared upstreams. Complete
commands preceding the interrupt retain their order; partial commands and
remaining bytes are discarded, and the interrupt is never forwarded upstream.

### Spot-focused status

`status` and periodic broadcasts provide a five-line overview: uptime, clients,
feed availability, spot totals, evaluated corrections and queue drops.
`longstatus` returns the detailed report, including:

- Parsed spots, selected output, suppressed repeats and rejected input.
- Frequency/callsign changes and admitted frequency-move updates.
- Per-source/receiver/band alignment: fresh usable distinct-call samples,
  retained samples, exclusion reasons, readiness, signed adjustment in Hz and
  age of the last usable reference match. `n/a` means unavailable;
  `+0.0 Hz` is a real usable zero adjustment. The adjustment is **added** to
  that receiver/representation's reported frequency.
- Per-feed processing counts, connection state and age of the last spot.
- Pending, confirmed, expired and conflicting movement proposals; retained
  evidence, capacity evictions and source resets. A public RBN status is bounded
  to 40 receiver/band rows, prioritizing ready/fresh evidence and reporting how
  many rows were omitted.

Counters cover process lifetime. Selected means admitted for fanout, not proof
of receipt by N1MM. Correction counts include evaluations later suppressed as
repeats. History and processing-bypass traffic is excluded from spot outcome
counts; received lines include all application lines. Login sent is not a claim
of authenticated readiness. Status reads do not learn or refresh calibration.

### Debug-verbose: one spot, one log line

With JSON configuration, enable this in your test configuration:

```json
"debug_verbose": true
```

Without `--config`, the equivalent switch is `--debug-verbose`. As with other
flags, JSON remains authoritative when `--config` is used. Ordinary `debug`
logging remains available separately; debug-verbose enables debug logging too.

Every incoming spot is printed to the console/log with decision details on its
right, separated by ` | `. The suffix includes source/role, SELECT/DROP/BYPASS,
reason, proposed frequency/callsign changes, GoCluster confidence when present,
anchor/candidate frequency, distinct corroboration support, reference basis,
and receiver-specific alignment state, fresh/retained sample count, exclusion
reason and adjustment when available. Suppressed
changes are explicitly marked evaluated-only. Each record is one physical line;
nonprintable bytes are escaped and long records may wrap visually in a narrow
terminal. Verbose diagnostics add work and log volume.

**These annotations never go to N1MM or upstream servers.** Original output
comments and Telnet application payloads remain unchanged. `debug_verbose` must
be a JSON boolean. Restart the foreground test after changing the setting.

## GoCluster connection settings

GoCluster is enabled at `cluster.n2wq.com:8300`, login `TK0C`, receive-only.
The configured connection sends **only the login callsign**. It sends no startup
commands, readback queries or filter changes, preserving the settings managed
directly on the server. Reconnects use the same login-only behavior.

`GO.settings` is retained for future use but is **not loaded or sent** by the
current JSON configuration. To opt into setup replay later, add
`settings_file: "GO.settings"` and a verified post-login `ready_text` to that
source. Do not enable these while relying on manually managed server settings.

For optional file-based setup, relative paths resolve against the JSON directory
(or working directory for CLI configuration). Files load when the script starts;
commands replay after readiness on each connection. Blank lines and `#` comment
lines are ignored. Files are limited to 16 KiB, 16 printable-ASCII commands and
512 characters per command. Use a settings file or inline startup commands, not
both. Missing or invalid enabled settings files fail before connections.

Reusing login TK0C may replace another active session with the same identity.
It is the explicitly requested login for this installation.

## Upgrade from the previous release

Save the previous code and its JSON together before changing deployment files.
The new processing path replaces dwell/limbo quality aggregation: primary spots
are no longer exempt from shared repeat suppression, and local singletons no
longer need two receivers. Repetition is not independent corroboration.

`spot_processing` must be a JSON object. For example, use
`"spot_processing": {"enabled": false}` to disable it; bare `false`, `null`,
arrays and other non-object values fail validation before connections start.

Use `spot_processing` for explicit units and policy. Historical `rbn_*` options
are legacy compatibility inputs, not a promise to recreate synthesized Q/Z
comments or old minimum-quality gating. `rbn_respot` supplies `repeat_seconds`
only when the latter is absent. `rbn_dedupe` no longer gates the new engine;
old dwell, minimum-quality and inrush settings no longer control admission.
Set `spot_processing.enabled` to false for framed forwarding without spot
processing; this bypasses corrections and rate suppression and is not identical
to the old aggregator. In particular the old `rbn_search_khz`
name described 0.1-kHz key increments: its default 5 searched approximately
±0.5 kHz. Do not migrate it by multiplying the effective merge radius tenfold.
The standalone `rbn_dedupe.py` file is retained for historical compatibility;
the upgraded relay uses the source-aware engine.

Add explicit roles and a dedicated GoCluster callsign to the JSON servers list.
Do not copy the former malformed fourth private address. Correct endpoint
addresses deliberately for the actual station. Review the new rate/correction
settings against representative captured lines before enabling production use.

## Validation and N1MM qualification

Run automated tests with Python 3.8+ without external services or third-party test dependencies. The suite uses `IsolatedAsyncioTestCase`. Production files were checked against Python 3.7 syntax; actual Python 3.7 runtime qualification has not been performed:

```sh
python3 -m unittest discover -s tests -v
PYTHONPYCACHEPREFIX=.pycache python3 -m py_compile spot-collector.py spot_processing.py stream_protocol.py
python3 -m json.tool spot-collector.json
```

Tests and a synthetic consumer establish only the tested software contracts.
They do not establish actual N1MM compatibility, oscillator accuracy or RF
performance. Follow [the manual qualification procedure](docs/N1MM-qualification.md)
for bandmap refresh, tuned frequency, comments, primary commands/history,
reference outages, reconnects and slow-client behavior.

## Deployment and rollback

The existing systemd unit starts `/opt/spot-collector/spot-collector.py` with
`/opt/spot-collector/spot-collector.json`. Deploy the complete matching release,
including its Python modules and reviewed configuration, using the station's
normal release process. This source change does not deploy or restart anything.

To roll back, restore the saved previous code **and matching configuration**,
then use the normal service process to restart it. Keep the listener and primary
endpoint unchanged so N1MM does not need a connection change. Removing GoCluster
alone disables that input; it does not restore the former aggregation semantics.
