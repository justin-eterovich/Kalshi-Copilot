"""Forecast-error calibration: how wrong is *this* station, at *this* lead?

Daily-temperature markets are priced from a National Weather Service forecast
plus a distribution around it. The forecast is given; the width of that
distribution is not, and it is the only free parameter in the whole engine.
This module measures it from the station's own history of (forecast, actual)
pairs instead of picking it.

**The dangerous version of this module returns a single global sigma.** It is
one number, it always answers, and it is wrong in both directions at once: too
narrow for a 5-day lead, too wide for a 6-hour one. Narrow-and-wrong prices the
far buckets with confidence nobody has earned; wide-and-wrong prices the near
buckets closer to a coin flip than they are. Those are the two places the money
is, so a global sigma is not a mild approximation — it is maximally wrong
exactly where it gets acted on. Worse, it is invisible: a bucket priced at 12c
off a fabricated sigma looks exactly like a bucket priced at 12c off a measured
one, all the way through the EV arithmetic and onto the proposal ticket.

The decisions that follow from that:

- **Bucketed by lead time, never pooled.** A forecast three days out and one
  made this morning are different instruments and are never mixed. Leads that
  fall outside the configured edges are dropped, not squeezed into the nearest
  bucket.
- **Sigma is the spread about the *measured bias*, not about zero.** See
  `calibrate` — this is the difference between a standard deviation and an
  RMSE, and getting it wrong both hides a correctable error and inflates the
  uncertainty.
- **Refusals return `None` and thin buckets carry a NaN sigma.** There is no
  default sigma, no fall back to a neighbouring bucket, no global prior.
  `weather.min_calibration_samples` (default 30) is the gate, and the correct
  response to a station below it is to decline to price the market.

**Known limitations, stated rather than hidden.** Errors are pooled across the
whole history a caller hands over, so a station whose upstream model changed
mid-sample is measured as the average of two different forecasters. Nothing
here is seasonal, and a station's summer skill is not its winter skill.
`sigma_f` is a spread, not a claim that the error is Gaussian — the consumer in
`distribution.py` makes that assumption and owns it. And this measures error
against whatever `actual_f` the caller supplies; Kalshi settles on a specific
official observation with its own rounding, and a calibration built against a
different series measures the wrong thing perfectly.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass

__all__ = [
    "DEFAULT_LEAD_EDGES",
    "ErrorStats",
    "ForecastError",
    "calibrate",
    "debias",
    "lead_bucket",
    "sigma_for",
]

#: Upper edges, in hours, of the lead-time buckets. Each bucket is
#: ``(previous_edge, edge]`` and is labelled by its upper edge, so bucket 24
#: holds forecasts issued 12 to 24 hours ahead. The spacing is roughly
#: geometric because forecast skill decays that way: the difference between a
#: 6-hour and a 12-hour lead is large, between 96 and 168 much less so.
DEFAULT_LEAD_EDGES: tuple[int, ...] = (6, 12, 24, 48, 96, 168)

#: A standard deviation needs two observations. One sample has no spread, and
#: reporting 0.0 for it would price every bucket at 0 or 1.
_MIN_FOR_SPREAD = 2


@dataclass(frozen=True, slots=True)
class ForecastError:
    """One (forecast, actual) pair for a station, with the lead it was made at.

    Temperatures are degrees Fahrenheit because that is what both the NWS point
    forecast and the Kalshi strike are quoted in; converting would only add a
    rounding step between two sources that already agree.
    """

    #: Hours between forecast issuance and the period it forecast. Never
    #: negative — see `lead_bucket`.
    lead_hours: float
    forecast_f: float
    actual_f: float

    @property
    def error_f(self) -> float:
        """**Forecast minus actual.** Positive means the forecast ran warm.

        The sign convention is fixed here and nowhere else. `debias` subtracts
        it for exactly this reason; see `ErrorStats.bias_f`.
        """
        return self.forecast_f - self.actual_f


@dataclass(frozen=True, slots=True)
class ErrorStats:
    """What one lead-time bucket's forecasts actually did."""

    #: Upper edge of the bucket, in hours. Bucket 24 is "12 to 24 hours out".
    lead_bucket_hours: int
    samples: int
    #: Mean signed error, in the **forecast-minus-actual** direction: positive
    #: means this station's forecasts at this lead run *warm* and the
    #: correction is to subtract. The two conventions differ only in sign, and
    #: a caller who applies this backwards doubles the error instead of
    #: removing it, so the direction is named here, in `ForecastError.error_f`,
    #: and in `debias`, and is asserted in the tests.
    bias_f: float
    #: Sample standard deviation of the error **about `bias_f`**, not about
    #: zero. `math.nan` when the bucket did not clear the sample floor it was
    #: calibrated with — see `calibrate` for why NaN and not 0.0.
    sigma_f: float
    #: Mean absolute error. Reported for the operator, not used in pricing:
    #: MAE is the more intuitive "how far off is it usually" number, but it is
    #: not the scale parameter of any distribution.
    mae_f: float

    @property
    def usable(self) -> bool:
        """Whether this bucket's sigma may be used to price anything.

        Necessary, not sufficient: it says the number is real, having cleared
        the floor `calibrate` was given and having actual spread. A caller must
        still gate on its *own* `min_samples`, which is what `sigma_for` does —
        stats can be persisted and reloaded under a changed config, and a sigma
        computed under a floor of 5 must not be spent under a floor of 30.

        A sigma of exactly 0.0 is refused here rather than passed on. It does
        not mean a perfect forecaster; on real data it means every error in the
        bucket is identical, which is a duplicated-row bug upstream. Priced, it
        puts every bucket at 0 or 1.
        """
        return (
            self.samples >= _MIN_FOR_SPREAD
            and math.isfinite(self.sigma_f)
            and self.sigma_f > 0.0
        )


def lead_bucket(
    lead_hours: float, *, edges: Sequence[int] = DEFAULT_LEAD_EDGES
) -> int | None:
    """Which lead-time bucket a forecast belongs to, or `None`.

    Buckets are half-open `(previous_edge, edge]` and labelled by `edge`, so a
    lead landing exactly on an edge takes the tighter bucket. A lead of 0 — a
    forecast for the period now beginning — is legitimate and lands in the
    first bucket.

    Three refusals, all of which would otherwise flatter the calibration:

    - **A negative lead.** A "forecast" timestamped after the period it
      describes is a join done against the wrong column, and it would report
      near-zero error at the shortest lead, which is precisely the bucket the
      engine trusts most.
    - **A lead past the last edge.** Folding a 300-hour forecast into the
      168-hour bucket understates that bucket's sigma with data from a lead it
      does not cover. Dropping the sample loses information; keeping it
      corrupts a bucket that gets priced.
    - **Edges that are empty, non-ascending, or non-positive.** A transposed
      config would otherwise silently bucket everything into one label.
    """
    previous = 0
    for edge in edges:
        if edge <= previous:
            return None
        previous = edge
    if previous == 0:  # empty `edges`
        return None

    if not math.isfinite(lead_hours) or lead_hours < 0.0:
        return None

    for edge in edges:
        if lead_hours <= edge:
            return edge
    return None


def calibrate(
    errors: Sequence[ForecastError],
    *,
    min_samples: int,
    edges: Sequence[int] = DEFAULT_LEAD_EDGES,
) -> list[ErrorStats]:
    """One `ErrorStats` per populated bucket, ascending by lead time.

    Every populated bucket is returned, including buckets far below
    `min_samples`, because the operator needs to see coverage — "this station
    has 400 samples at 6h and 3 at 96h" is the useful diagnostic, and dropping
    the thin rows would render it as silence. What the floor controls is
    `sigma_f`: below `max(min_samples, 2)` it is `math.nan` and `usable` is
    False.

    **NaN rather than 0.0, deliberately.** Both are wrong answers, but a NaN
    propagates into a visibly broken price and compares False against every
    threshold on the way, whereas a 0.0 sigma prices every bucket at exactly 0
    or 1 and looks like an enormous, tradeable edge. `bias_f` and `mae_f` are
    still reported for thin buckets — they are defined for a single
    observation and are diagnostics, not pricing inputs.

    **Sigma is `statistics.stdev`: the spread about the sample mean (which is
    `bias_f`), using n-1.** Two choices in one line, both load-bearing:

    - *About the mean, not about zero.* `sqrt(mean(e**2))` is the RMSE, and it
      folds a station's systematic bias into its uncertainty. A station running
      2°F warm with a true spread of 3°F would report ~3.6°F — hiding an error
      that `debias` can simply remove, and widening every bucket towards a coin
      flip in the process. The bias and the spread are separate facts and are
      corrected separately.
    - *n-1, not n.* These are samples of an ongoing process, not an enumerated
      population. The population form is biased low, and low is the dangerous
      direction here: it overprices confidence.

    Samples are dropped, never repaired, when the lead has no bucket (see
    `lead_bucket`) or when any of the three numbers is non-finite. A NaN
    temperature from a gap in the observation series would otherwise poison a
    whole bucket's mean and standard deviation to NaN.

    `edges` must be the same edges later passed to `sigma_for`; the bucket
    labels are how the two agree, and mismatched edges produce a refusal rather
    than a wrong sigma.
    """
    grouped: dict[int, list[float]] = {}
    for err in errors:
        if not (
            math.isfinite(err.lead_hours)
            and math.isfinite(err.forecast_f)
            and math.isfinite(err.actual_f)
        ):
            continue
        bucket = lead_bucket(err.lead_hours, edges=edges)
        if bucket is None:
            continue
        grouped.setdefault(bucket, []).append(err.error_f)

    floor = max(min_samples, _MIN_FOR_SPREAD)
    stats: list[ErrorStats] = []
    for bucket in sorted(grouped):
        values = grouped[bucket]
        n = len(values)
        # `stdev` centres on the sample mean, which is `bias_f` — that is the
        # "about the measured bias" property, not an incidental detail.
        sigma = statistics.stdev(values) if n >= floor else math.nan
        stats.append(
            ErrorStats(
                lead_bucket_hours=bucket,
                samples=n,
                bias_f=statistics.fmean(values),
                sigma_f=sigma,
                mae_f=statistics.fmean([abs(v) for v in values]),
            )
        )
    return stats


def sigma_for(
    stats: Sequence[ErrorStats],
    lead_hours: float,
    *,
    min_samples: int,
    edges: Sequence[int] = DEFAULT_LEAD_EDGES,
) -> float | None:
    """The sigma to price a forecast at `lead_hours` with, or `None`.

    `None` is the expected answer for a station the system has not verified
    against, and it means *decline to price this market*. It is never a
    suggestion to substitute something. In particular there is **no fall back
    to a neighbouring bucket**: if the 24-hour bucket is empty, the 48-hour
    sigma is not an approximation of it, it is a different instrument's
    uncertainty wearing the right units. And there is no global default, which
    is the failure this whole module exists to prevent.

    Refuses when the lead has no bucket, when no bucket in `stats` carries that
    label, when the bucket is below `min_samples`, or when its sigma is not a
    finite positive number.

    Also refuses when two entries share a bucket label. `calibrate` cannot
    produce that, so it means someone concatenated the calibrations of two
    different stations — picking either one would price a market against a
    station a thousand miles away.
    """
    bucket = lead_bucket(lead_hours, edges=edges)
    if bucket is None:
        return None

    matches = [s for s in stats if s.lead_bucket_hours == bucket]
    if len(matches) != 1:
        return None
    stat = matches[0]

    # Re-applied here rather than trusted from `calibrate`: this list may have
    # been persisted under a different config.
    if stat.samples < max(min_samples, _MIN_FOR_SPREAD):
        return None
    if not stat.usable:
        return None
    return stat.sigma_f


def debias(forecast_f: float, stats: ErrorStats) -> float:
    """Correct a raw forecast by this bucket's measured bias.

    `bias_f` is **forecast minus actual**, so the correction **subtracts**: a
    station whose forecasts run 2°F warm has `bias_f == +2.0`, and the
    debiased forecast is 2°F cooler. Adding instead of subtracting does not
    produce a slightly worse number, it produces one twice as wrong as the raw
    forecast, in the direction the data said it was already wrong.

    Deliberately total — it returns a float and never refuses. A mean is
    defined for a single observation, and this is arithmetic on a number the
    caller already holds. The gate belongs at `sigma_for`: without a sigma
    there is no price, so a bias from a bucket too thin to trust never reaches
    a ticket.
    """
    return forecast_f - stats.bias_f
