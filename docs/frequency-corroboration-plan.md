# Frequency corroboration and automatic relative alignment — implementation plan

Implementation approved by the owner on 2026-11-17. This document records the selected contract; test and release evidence must establish completion.

## Confirmed operating policy

- Deliver the first valid spot promptly.
- Moving an established frequency requires two distinct receiver identities, including moves reported locally.
- Two suffice without a majority. Either incoming feed can supply evidence; mirrored reports of one receiver count once.
- Prefer corroborated GoCluster frequency information; a single GoCluster report is not sufficient authority for a move.
- Unconfirmed candidates expire without delivery. No timeout-based forced release.
- Add automatic adjustment for remote receiver/feed-output disagreement, beyond existing local-only alignment.
- Preserve source comments, bounded traffic, the existing primary command route, and login-only TK0C GoCluster configuration. No hardware settings or GoCluster code changes.

## Current gap and HA8LNN acceptance case

Current movement admission only checks displacement, timestamp order and the two-update budget. Local alignment runs only for role=local; GoCluster frequency enters unchanged. Thus IZ3EAW's +700-Hz report escaped suppression even though many other receivers agreed at 7009.00.

Replay the supplied HA8LNN episode as a regression: initial 7009.00 passes; ordinary 7009.00 reports build evidence; IZ3EAW's 7009.70 remains pending with one receiver; HB9FX at 7009.00 is a repeat. Neither consumes a movement update. This episode alone must not establish a persistent IZ3EAW adjustment.

## Proposed behavior and remaining decision

Separate receiver observations, a delivered frequency anchor, and pending movement proposals. Keep each receiver's latest eligible observation for the call/band/mode; repeated reports refresh one receiver rather than add votes. Observe before output suppression. A promoted move consumes one existing update allowance and changes the anchor; pending/rejected proposals consume none. Old pre-promotion support must not be reused automatically to confirm a return move.

Use explicit bounded candidate lifetime and agreement tolerance settings. Initial engineering proposals are a 10-second evidence/candidate interval and a maximum 100-Hz spread within an agreeing pair/group; these are proposed policy, not measured RF accuracy. Preserve the existing 500-Hz movement trigger, 180-second repeat lifetime and two-update budget initially. Test exact boundaries and quantized readings before selecting final settings.

Owner selected the second conflict option: if incompatible groups each reach two receivers, prefer the corroborated GoCluster-supported group. A lone GoCluster outlier still cannot defeat a two-receiver group. A tie between equally qualified GoCluster-supported groups remains insufficient to select a new anchor without an explicit deterministic policy; retain the anchor pending resolution rather than choosing by packet arrival.

## Automatic adjustment constraints

A single frequency event can confirm a move but cannot establish a reusable receiver adjustment. Require repeated qualified comparisons across different calls and time, exclude the receiver being adjusted and its identifiable mirrors from its reference, and keep original unadjusted observations. Prefer qualified GoCluster evidence without treating feed priority as an additional receiver.

Track applicability by receiver identity, band and input representation/route: direct raw output and GoCluster-processed output can have different transformations. Do not apply a raw receiver adjustment a second time to an already corrected feed. Unknown aggregate provenance must not be expanded into hidden receiver votes. Known aliases need explicit normalization; do not collapse distinct SSIDs into positive support by assumption.

Automatic output adjustment is conditional on adequate, fresh evidence and expires/invalidate on source changes. Insufficient evidence retains the original frequency and applies ordinary move corroboration. Corrections operate on Collector output only, never hardware/RBN publishing. Relative alignment is not absolute calibration; hidden upstream contributors limit proof of independence.

## Work slices

1. Evidence and replay tests (`spot_processing.py`, new corroboration tests): bounded receiver/episode observations, route-copy deduplication, expiry and deterministic clocks; preserve existing output until move policy is switched. Capture baseline output/state and performance for comparison.
2. Corroborated moves: pending groups, distinct support, promotion/expiry, anchor generations and update budget. Integrate only after the competing-group decision is resolved. Preserve first-spot admission and comments.
3. Automatic remote alignment: extend qualified receiver/representation ownership using existing alignment safeguards where valid. Do not loosen existing 500-Hz observation matching to admit +700-Hz training blindly: a wider diagnostic/calibration comparison window must have separate same-event and ambiguity gates. Test learned adjustments on held-out calls, not only training observations.
4. Diagnostics/configuration (`spot-collector.py`, `status_report.py`, JSON and README): debug-verbose shows anchor, candidate frequency, distinct support, reference basis, confirmation/expiry/conflict, and applied offset. Status summarizes pending/confirmed/expired proposals and qualified adjustments. No spot comment additions or startup commands.
5. Independent final review and qualification: regressions, baseline differential replay for unaffected behavior, finite retained-state/churn tests and populated-state benchmarks. Use bounded indexes and eviction coupling rather than per-line global scans. No deployment or production connections as part of implementation tests.

## Independent reference cases

A separate non-steered scientific review received the user policies and observed episode, without a preferred implementation. It established the following mathematical/policy cases; it did not establish empirical thresholds.

| Case (frequencies kHz) | Expected result | Basis |
| --- | --- | --- |
| First A=7009.700 | Immediate delivery | First-spot policy |
| Anchor 7009.000; A=7009.700 delivered through two feeds | One witness; no promotion | Receiver identity |
| Anchor 7009.000; fresh A=B=7009.700; seven others at 7009.000 | Candidate meets two-receiver condition | No-majority policy |
| A and B agree at 7009.000; GC C alone says 7009.700 | No move to C | GC priority creates no extra evidence |
| One call gives A-reference=+700 Hz | No transferable adjustment | One event cannot identify persistent bias |
| Repeated qualified events show +700 Hz relative residual | Candidate output adjustment −700 Hz | Subtraction; empirical gates still required |
| One of two witnesses expires | One usable witness | Freshness |
| Unconfirmed candidate expires | No output/update-budget consumption | Expiry policy |

Additional required tests: legitimate move with two fresh receivers; old reports after promotion; same-minute timing; aliases/SSID uncertainty; cross-band isolation; processed aggregate copies; decoder errors across calls; reference outages; reconnect; stable offset versus drifting offset and scale-like differences; exact original suffix preservation; bounded state after churn.

The review's strongest supported claim is conditional relative alignment to qualified references. Actual IZ3EAW bias, optimal timing/tolerance, correction accuracy and production capacity remain unverified. Two agreeing new reports can still be delayed or correlated; corroboration reduces isolated outliers but does not prove RF truth or transmission order.

## Live-test sample diagnosis (2026-11-17)

The current test configuration labels public `telnet.reversebeacon.net:7000` as local Server2. Each raw RBN origin is consequently registered as a local receiver. Global self-return exclusion then rejects GoCluster observations from any of those origins, including otherwise independent receivers. This is appropriate protection against counting one's own mirror but not an appropriate population model for a public multi-receiver feed. A deterministic probe produced one eligible match; observing its reference receiver on another raw RBN call reduced displayed samples to zero while the retained sample remained. This explains retained samples versus 0/5/never.

Current local observations and sample keys also use the input endpoint rather than the physical receiver (source/call/band/mode and source/band/call respectively). Different RBN receivers therefore overwrite each other's observations for the same call. Fix receiver/representation ownership and subject-specific mirror exclusion before extending automatic remote adjustment. Support a raw multi-receiver feed explicitly rather than treating public RBN as one local oscillator. Never estimate a shared offset for all RBN receivers.

The displayed count is currently eligible distinct calls, not a lifetime packet count. Freshness, five distinct calls, time/frequency coverage, self-exclusion and reference availability still apply. Diagnostics should explain rejection causes (self/mirror, missing or aged reference, time/frequency mismatch, insufficient distinct calls) and distinguish retained versus usable samples. Do not lower evidence requirements simply to make the number rise.

Add regressions for an entire public RBN feed, same receiver mirrored through both clusters, a different raw receiver usable as independent reference, per-receiver same-call isolation, rolling expiry, and status reporting that explains retained-but-ineligible matches. Implementation includes explicit raw-feed normalization, per-receiver ownership and diagnostics. User-owned test configuration is not rewritten.
