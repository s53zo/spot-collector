# N1MM qualification and rollback

Automated tests exercise synthetic spot consumers and fake/local transports.
They do not establish compatibility with a particular N1MM version, radio CAT
setup, RF calibration or a production cluster. Complete this procedure on a
staging installation before using the upgraded collector during a contest.

## Prepare a controlled trial

1. Record the N1MM version, selected contest, Telnet cluster type, automatic
   connection commands, filters, spot lifetime and network-master settings.
2. Record whether N1MM randomizes incoming spot frequencies. Disable this for
   frequency-comparison measurements, or account for its known transformation.
3. Save the previous collector release and its JSON configuration together.
   Keep the N1MM endpoint unchanged during the trial. Use a separate test port
   and controlled upstreams rather than publishing synthetic DX spots publicly.
4. Use a dedicated numeric-SSID GoCluster login/profile. Configure its desired
   CW, band and geographic filters directly on GoCluster; commands typed through
   the collector remain routed to the existing primary. Check other saved PASS
   rules because they can broaden GoCluster selection.
5. Keep all reference receivers and their routes identifiable in the capture.
   Record complete raw lines, upstream source, receive time, receiver callsign,
   collector output and any known RF reference measurement. Protect private
   endpoint information when sharing captures.

## Observe N1MM, not only collector output

Use distinct test calls and known frequencies so existing bandmap entries do not
hide a failure. Check the actual Bandmap and Available Mults & Qs windows as well
as the Telnet window.

| Stimulus | Expected observation |
| --- | --- |
| One valid local CW spot, no external confirmation | Prompt delivery, including while frequency adjustment is unavailable; original comment unchanged. |
| Same local report repeated directly and returned through a cluster | Repetitive traffic is suppressed, without manufacturing independent receiver support. |
| First local spot, then matching GoCluster spot | No special enrichment-only bypass during suppression; later permitted output may use eligible GoCluster information. |
| Reported frequency movement beyond configured tolerance | One receiver stays pending; two distinct receivers can admit a bounded movement update before ordinary suppression expires. Check which N1MM entry changes and whether an obsolete entry remains. |
| Fresh local report followed by an older GoCluster frequency | The stale report must not pull the station back to an obsolete frequency. |
| Local callsign resembling a nearby external call | Only a unique one-character replacement supported by two distinct external receivers can replace the local call; competing original-call support blocks it. Verify whether N1MM removes or retains an earlier entry; a new spot is not a deletion command. |
| Signed, zero or absent report and numeric human text | No sign change or invented signal report; comment and timestamp remain intact. |
| GoCluster line containing grid/confidence and long comment | Forwarded suffix remains intact; no Collector Q/Z or correction-warning text appears. |

Select a spot and check the tuned radio frequency. A number displayed to 10 Hz
is not evidence of 10-Hz RF accuracy. Compare original source frequency,
collector output, N1MM bandmap frequency and radio setting separately.

## Exercise references and failure behavior

- Start without GoCluster. Local spots must continue without a calibration
  warm-up warning or hold. Connect and disconnect the reference during a run.
- Feed several controlled same-transmission comparisons across different calls,
  then slow synthetic drift. Confirm adjustments are per receiver/band/input representation and
  stop being used when reference evidence expires.
- Repeat the same receiver through several feed routes. These copies must not
  satisfy independent-evidence requirements.
- Introduce a real transmitter frequency move and delayed old reports. Neither
  may be learned as a receiver-wide drift adjustment.
- Reconnect a local source and verify stale source evidence is not inherited
  indiscriminately. Reconnect GoCluster and verify its login identity/profile.
- Fragment a spot, login prompt and Telnet negotiation across reads; combine
  several lines in a read. Compare complete downstream lines with whole-line
  delivery.
- Include an unterminated structured command prompt immediately before the first
  DX line, a timestamp followed by a grid, and escaped IAC bytes. Confirm the
  first spot remains standalone and its decoded comment is unchanged.
- Stall a client until graceful close times out. Confirm forced closure retires
  its socket and frees the client slot. Simulate a failed upstream login write
  and confirm the connection closes and reconnects without sending extra commands.
- Pause or stop one N1MM client while another remains active. Check the healthy
  client and upstreams continue, and inspect queue-drop/disconnection status.
- From N1MM issue a harmless primary command and a history query. Verify primary
  responses remain useful and GoCluster receives no logger commands. During a
  control test with an isolated primary, verify manual DX routing; do not publish
  fabricated test spots on production networks.

## Judge usefulness

Compare identical recorded inputs through the previous and candidate releases.
Count first delivered call/band/frequency episodes, delivery delay, repeats,
missed opportunities, field changes and independently checked wrong calls.
Inspect baseline-only and candidate-only cases. Fewer spots alone is not a win;
a missed or unworked station alone is not proof of a bad spot.

Automatic frequency alignment uses GoCluster as an operational reference. Its
frequencies may already be corrected or averaged, and its hidden contributors
can include local reports returned through RBN. The trial must not be described
as absolute calibration or independent receiver verification without additional
reference measurements.

## Roll back

Stop the staging/candidate process using the deployment's normal service control,
restore the saved previous release **and its matching configuration**, and start
that release. Keep the listener address/port and original primary unchanged so
N1MM needs no endpoint migration. Merely removing GoCluster from the upgraded
configuration removes that input; it does not restore all previous aggregation
behavior. Recheck primary command routing and local spot receipt after rollback.

## Corroboration and raw multi-receiver qualification

- Replay HA8LNN at 7009.00, one IZ3EAW report at 7009.70, then HB9FX at
  7009.00. The outlier stays pending; the return is a repeat; no move allowance
  is consumed and no persistent receiver adjustment follows from this one call.
- Have two distinct receivers agree on a real new frequency within the configured
  window. Confirm one move passes even if more receivers still report the old
  anchor. Return moves need fresh evidence, not reused pre-promotion votes.
- Send the same origin through raw RBN, the primary and GoCluster. It counts
  once. Confirm two qualified conflicting groups prefer the GoCluster-supported
  group, while equally qualified GoCluster conflicts retain the current anchor.
- Test the public RBN entry as `rbn`; confirm a legacy `local` entry for the
  known public hostname produces a warning and normalizes without editing JSON.
  Different receivers must have separate sample/adjustment rows.
- Use repeated qualified events across calls/time/frequency to train a relative
  receiver offset, then test on held-out calls and expire the reference. Check
  raw and processed routes do not apply the same correction twice.
- Inspect fresh/retained samples and exclusion reasons. A retained-but-ineligible
  match must not appear as usable evidence; increasing packet count alone does
  not increase distinct-call coverage. Public status limits rows and reports
  omitted receivers; verbose traces remain one physical line.

## Local call hold qualification

- Replay S53M at 7010.0 from two independent external receivers, then local
  S53MM at 7010.1. Check the selected identity and exact unchanged local comment.
- Repeat one external receiver through two routes: this is one witness, not two.
- With one external candidate, check HOLD. Add the second before the deadline
  for early resolution, then repeat without adding evidence and verify the
  original call releases after three seconds even during otherwise quiet input.
- Support the original S53MM externally or introduce a second plausible nearby
  replacement. Verify no unsafe correction. Check three-character and portable
  identities, stale evidence, no candidate, hold-cap overflow and reconnect.
- Compare receiver observations/calibration sample timestamps before and after
  repeated rechecks: holds must not teach a new observation on each timer tick.
