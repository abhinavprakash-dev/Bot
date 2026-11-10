"""Risk scoring and threshold engine.

Two independent ideas:

* **Thresholds** answer "has this actor done too much too fast?". They count
  actions inside a sliding window per (guild, actor, protection) and per
  (guild, actor) across *all* protections, so splitting an attack over five
  different protections does not buy the attacker extra time.
* **Risk scores** answer "how bad is this actor overall?". A score accumulates
  over a decaying window so an attacker who pauses for a minute decays back
  down, while a slow drip of escalations stays hot.

Both are pure: they take timestamps and return numbers, so they can be tested
without a Discord connection.
"""
from __future__ import annotations

import math
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from . import rules

# Default per-action threshold. Servers can override any of these.
DEFAULT_THRESHOLDS: dict[str, int] = {
    "antichannel": 2,
    "antirole": 2,
    "antiban": 3,
    "antibot": 1,
    "antiwebhook": 2,
    "antiserver": 1,
    "antiintegration": 1,
    "antimention": 4,
    "antipromo": 1,
    "antilink": 5,
    "anticaps": 5,
    "antispam": 6,
    "antiraid": 3,
    "antialt": 1,
    "antipermission": 1,
    "antiadmingrant": 1,
    "antirolepermedit": 1,
    "antirolehoist": 2,
    "antichannelpermedit": 1,
    "antichannelrename": 3,
    "antirolerename": 3,
    "antiemojidelete": 1,
    "antiemojicreate": 3,
    "antistickerdelete": 1,
    "antistickercreate": 2,
    "antivanity": 1,
    "antiicon": 1,
    "antiname": 1,
    "anticommunity": 1,
    "antiautomod": 1,
    "antiinvitecreate": 3,
    "antiinvitedelete": 2,
    "antisoundboard": 3,
    "antistage": 2,
}

DEFAULT_WINDOW = 10.0
#: Actions inside this window across *all* protections count as a coordinated
#: burst even when each individual protection is still under its own limit.
BURST_WINDOW = 15.0
BURST_LIMIT = 6
#: Reaching this risk score triggers lockdown instead of a single punishment.
LOCKDOWN_SCORE = 120.0
#: A lockdown also needs real volume. Without this floor, two CRITICAL events
#: (for example two bots added back to back) would trip it immediately, which
#: is both a false positive and an easy way to grief a server into lockdown.
LOCKDOWN_MIN_ACTIONS = 3
#: Minimum seconds between two punishments of the same member.
PUNISH_COOLDOWN = 30.0
#: Maximum punishments in a short rolling period before lockdown.
BURST_PUNISH_LIMIT = 4
BURST_PUNISH_WINDOW = 60.0

#: Risk contribution per detection severity.
SEVERITY_WEIGHT = {
    rules.LOW: 4.0,
    rules.MEDIUM: 12.0,
    rules.HIGH: 25.0,
    rules.CRITICAL: 45.0,
}
#: Escalation (handing over control) is worth more than a cosmetic change.
ESCALATION_BONUS = 30.0
#: Instant protections are, by definition, the ones that matter most.
INSTANT_BONUS = 8.0


@dataclass
class Verdict:
    """What the engine decided about one detection."""

    action: str  # allow | warn | punish | lockdown
    reason: str
    risk_score: float
    count: int
    limit: int
    burst_count: int = 0
    escalation: bool = False
    severity: str = rules.MEDIUM

    @property
    def punish(self) -> bool:
        return self.action in ("punish", "lockdown")

    @property
    def emergency(self) -> bool:
        return self.action == "lockdown"


@dataclass
class _ScoreBucket:
    """Decaying risk score for one (guild, actor)."""

    #: (timestamp, risk added)
    hits: deque[tuple[float, float]] = field(default_factory=deque)
    total: float = 0.0

    def add(self, moment: float, amount: float) -> None:
        self.hits.append((moment, amount))
        self.total += amount

    def decay(self, moment: float, half_life: float = 90.0) -> float:
        """Drop hits that are older than two half-lives and rescale the rest."""
        cutoff = moment - half_life * 2
        while self.hits and self.hits[0][0] < cutoff:
            self.hits.popleft()
        if not self.hits:
            self.total = 0.0
            return 0.0
        # Linear fade so the score visibly cools instead of dropping off a cliff.
        self.total = sum(amount * _fade(moment - stamp, half_life) for stamp, amount in self.hits)
        return self.total

    def clear(self) -> None:
        self.hits.clear()
        self.total = 0.0


def _fade(age: float, half_life: float) -> float:
    """1.0 for a fresh hit, decaying linearly to 0.15 at two half-lives."""
    if age <= 0:
        return 1.0
    return max(0.15, 1.0 - (age / (half_life * 2)) * 0.85)


def risk_weight(detection: rules.Detection) -> float:
    """Risk points a single detection contributes."""
    weight = SEVERITY_WEIGHT.get(detection.severity, SEVERITY_WEIGHT[rules.MEDIUM])
    # Every registry-built detection repeats the protection's
    # instant flag, so this is equivalent to reading the
    # protection while also honouring graduated detections
    # (member role grants) that deliberately act non-instantly.
    if detection.instant:
        weight += INSTANT_BONUS
    if detection.escalation:
        weight += ESCALATION_BONUS
    # A rename to something abusive is worth more than a tidy rename.
    for note in detection.notes:
        if "abusive" in note:
            weight += 10.0
    return weight


class RiskEngine:
    """Sliding-window thresholds plus a decaying per-actor risk score."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        threshold_reader: Optional[Callable[[int], dict]] = None,
        window_reader: Optional[Callable[[int], float]] = None,
        cooldown: float = PUNISH_COOLDOWN,
        lockdown_score: float = LOCKDOWN_SCORE,
    ) -> None:
        self.clock = clock
        self._threshold_reader = threshold_reader or (lambda _guild_id: DEFAULT_THRESHOLDS)
        self._window_reader = window_reader or (lambda _guild_id: DEFAULT_WINDOW)
        self.cooldown = cooldown
        self.lockdown_score = lockdown_score

        #: (guild, actor, protection) -> timestamps inside the window
        self.buckets: dict[tuple[int, int, str], deque[float]] = defaultdict(deque)
        #: (guild, actor) -> every timestamp, for the cross-protection burst check
        self.actor_buckets: dict[tuple[int, int], deque[float]] = defaultdict(deque)
        #: (guild, actor) -> decaying score
        self.scores: dict[tuple[int, int], _ScoreBucket] = defaultdict(_ScoreBucket)
        #: (guild, actor) -> last punishment time
        self.last_punish: dict[tuple[int, int], float] = {}
        #: (guild, actor) -> punishment timestamps
        self.punish_times: dict[tuple[int, int], deque[float]] = defaultdict(deque)
        #: (guild,) -> processed audit entry ids, so one entry can never punish twice
        self._seen: dict[int, deque[int]] = defaultdict(deque)
        self._seen_limit = 2000

    # ------------------------------------------------------------- thresholds
    def threshold(self, guild_id: int, module: str) -> int:
        table = self._threshold_reader(guild_id) or DEFAULT_THRESHOLDS
        try:
            value = int(table.get(module, DEFAULT_THRESHOLDS.get(module, 2)))
        except (TypeError, ValueError):
            value = DEFAULT_THRESHOLDS.get(module, 2)
        return max(1, min(20, value))

    def window(self, guild_id: int) -> float:
        try:
            value = float(self._window_reader(guild_id))
        except (TypeError, ValueError):
            value = DEFAULT_WINDOW
        return max(2.0, min(120.0, value))

    # ------------------------------------------------------------ seen guards
    def mark_entry(self, guild_id: int, entry_id: Optional[int]) -> bool:
        """Return True the first time this audit entry is seen.

        The gateway can deliver an audit entry twice after a reconnect. Without
        this guard a single action could trigger two punishments.
        """
        if entry_id is None:
            return True
        seen = self._seen[guild_id]
        if entry_id in seen:
            return False
        seen.append(entry_id)
        while len(seen) > self._seen_limit:
            seen.popleft()
        return True

    def forget_actor(self, guild_id: int, user_id: int) -> None:
        for key in [key for key in self.buckets if key[0] == guild_id and key[1] == user_id]:
            self.buckets.pop(key, None)
        self.actor_buckets.pop((guild_id, user_id), None)
        self.scores.pop((guild_id, user_id), None)
        self.punish_times.pop((guild_id, user_id), None)
        self.last_punish.pop((guild_id, user_id), None)

    def clear_guild(self, guild_id: int) -> None:
        for key in [key for key in self.buckets if key[0] == guild_id]:
            self.buckets.pop(key, None)
        for key in [key for key in self.actor_buckets if key[0] == guild_id]:
            self.actor_buckets.pop(key, None)
        for key in [key for key in self.scores if key[0] == guild_id]:
            self.scores.pop(key, None)
        for key in [key for key in self.last_punish if key[0] == guild_id]:
            self.last_punish.pop(key, None)
        for key in [key for key in self.punish_times if key[0] == guild_id]:
            self.punish_times.pop(key, None)

    # -------------------------------------------------------------- judgement
    def score_of(self, guild_id: int, user_id: int) -> float:
        return self.scores[(guild_id, user_id)].decay(self.clock())

    def evaluate(
        self,
        guild_id: int,
        user_id: int,
        detection: rules.Detection,
        *,
        trusted: bool = False,
        lockdown_active: bool = False,
    ) -> Verdict:
        """Decide what to do about one detection."""
        moment = self.clock()
        # The detection carries its own instant flag and may also
        # override the limit and window: the graduated member
        # role-grant path sets all three, while every
        # registry-built detection repeats the protection's values,
        # so existing behaviour is unchanged.
        instant = bool(getattr(detection, "instant", False))

        # A whitelisted actor keeps a higher bar, but is never fully exempt. An
        # escalation (handing over control) or an instant protection such as
        # "bot added" or "Administrator granted" still acts immediately, because
        # a compromised "trusted" account is exactly how a real attack starts.
        limit_override = getattr(detection, "limit", None)
        if limit_override is not None:
            limit = max(1, min(20, int(limit_override)))
        else:
            inflate = trusted and not detection.escalation and not instant
            limit = max(3, self.threshold(guild_id, detection.module) * 3) if inflate else (
                1 if instant else self.threshold(guild_id, detection.module)
            )
        window_override = getattr(detection, "window", None)
        if window_override is not None:
            window = max(2.0, min(120.0, float(window_override)))
        else:
            window = self.window(guild_id)

        bucket = self.buckets[(guild_id, user_id, detection.module)]
        bucket.append(moment)
        while bucket and moment - bucket[0] > window:
            bucket.popleft()

        actor_bucket = self.actor_buckets[(guild_id, user_id)]
        actor_bucket.append(moment)
        while actor_bucket and moment - actor_bucket[0] > BURST_WINDOW:
            actor_bucket.popleft()

        weight = risk_weight(detection)
        score = self.scores[(guild_id, user_id)]
        score.add(moment, weight)
        current = score.decay(moment)

        count = len(bucket)
        burst = len(actor_bucket)

        # Emergency first: a coordinated attack must reach lockdown even when the
        # per-protection cooldown is already suppressing ordinary punishments,
        # otherwise spamming one action forever is a free bypass.
        if burst >= LOCKDOWN_MIN_ACTIONS and (
            current >= self.lockdown_score
            or (burst >= BURST_LIMIT and current >= self.lockdown_score * 0.6)
        ):
            self._record_punish(guild_id, user_id, moment)
            self.clear_actor_buckets(guild_id, user_id)
            return Verdict(
                action="lockdown",
                reason=(
                    f"Risk score reached {current:.0f} across {burst} actions in "
                    f"{int(BURST_WINDOW)}s, which is treated as a coordinated attack."
                ),
                risk_score=current,
                count=count,
                limit=limit,
                burst_count=burst,
                escalation=True,
                severity=rules.CRITICAL,
            )

        if self._on_cooldown(guild_id, user_id, moment):
            return Verdict(
                action="allow",
                reason="Already punished for this; reverting silently to avoid a ban loop.",
                risk_score=current,
                count=count,
                limit=limit,
                burst_count=burst,
                escalation=detection.escalation,
                severity=detection.severity,
            )

        if instant and count >= limit:
            self._record_punish(guild_id, user_id, moment)
            bucket.clear()
            return Verdict(
                action="punish",
                reason=detection.reason,
                risk_score=current,
                count=count,
                limit=limit,
                burst_count=burst,
                escalation=detection.escalation,
                severity=detection.severity,
            )

        if count >= limit:
            # Cross-protection burst: punish even if this protection is alone
            # under its own limit, because the pattern is the signal.
            coordinated = burst >= BURST_LIMIT and detection.severity in (rules.HIGH, rules.CRITICAL)
            if coordinated or not instant:
                self._record_punish(guild_id, user_id, moment)
                bucket.clear()
                return Verdict(
                    action="punish",
                    reason=detection.reason,
                    risk_score=current,
                    count=count,
                    limit=limit,
                    burst_count=burst,
                    escalation=detection.escalation,
                    severity=detection.severity,
                )

        if count == max(1, limit - 1) and limit > 1:
            return Verdict(
                action="warn",
                reason=f"{count}/{limit} actions in {int(window)}s. One more and the protection acts.",
                risk_score=current,
                count=count,
                limit=limit,
                burst_count=burst,
                escalation=detection.escalation,
                severity=detection.severity,
            )

        if lockdown_active:
            return Verdict(
                action="punish",
                reason="Server is in lockdown: " + detection.reason,
                risk_score=current,
                count=count,
                limit=limit,
                burst_count=burst,
                escalation=detection.escalation,
                severity=detection.severity,
            )

        return Verdict(
            action="allow",
            reason=detection.reason,
            risk_score=current,
            count=count,
            limit=limit,
            burst_count=burst,
            escalation=detection.escalation,
            severity=detection.severity,
        )

    def clear_actor_buckets(self, guild_id: int, user_id: int) -> None:
        for key in [key for key in self.buckets if key[0] == guild_id and key[1] == user_id]:
            self.buckets[key].clear()
        self.actor_buckets[(guild_id, user_id)].clear()

    def _on_cooldown(self, guild_id: int, user_id: int, moment: float) -> bool:
        last = self.last_punish.get((guild_id, user_id))
        if last is not None and moment - last < self.cooldown:
            return True
        recent = self.punish_times[(guild_id, user_id)]
        while recent and moment - recent[0] > BURST_PUNISH_WINDOW:
            recent.popleft()
        return len(recent) >= BURST_PUNISH_LIMIT

    def _record_punish(self, guild_id: int, user_id: int, moment: float) -> None:
        self.last_punish[(guild_id, user_id)] = moment
        self.punish_times[(guild_id, user_id)].append(moment)

    # ------------------------------------------------------- raid / join risk
    def join_risk(self, guild_id: int, age_days: float, similar_joins: int) -> float:
        """Risk for a joining account, without punishing new accounts outright."""
        score = 0.0
        if age_days < 1:
            score += 30.0
        elif age_days < 3:
            score += 18.0
        elif age_days < 7:
            score += 8.0
        score += min(40.0, similar_joins * 6.0)
        return score


def severity_from_score(score: float) -> str:
    """Map a raw risk score onto the four severity bands used in the logs."""
    if score >= 100:
        return rules.CRITICAL
    if score >= 50:
        return rules.HIGH
    if score >= 20:
        return rules.MEDIUM
    return rules.LOW


def confidence_from_counts(count: int, limit: int) -> float:
    """How sure we are that a pattern is an attack rather than normal use."""
    if limit <= 0:
        return 0.0
    ratio = count / limit
    return max(0.0, min(1.0, (ratio - 1) / 2 + 0.5)) if ratio > 1 else max(0.0, min(1.0, ratio * 0.5))


def entropy_of(names: list[str]) -> float:
    """Detect 'admin123', 'admin124' style username floods.

    Returns 0.0 for a normal spread of names and approaches 1.0 when every name
    is a small mutation of the same base string.
    """
    cleaned = [rules.normalise_name(name).replace(" ", "") for name in names if name]
    if len(cleaned) < 3:
        return 0.0
    groups: dict[str, int] = defaultdict(int)
    for name in cleaned:
        groups[_shape(name)] += 1
    biggest = max(groups.values())
    # Very short bases are the real signal: "u1", "u2" ... or "rahul1", "rahul2".
    similarity = biggest / len(cleaned)
    base_length = max((len(key) for key in groups), default=0)
    if base_length <= 6 and similarity >= 0.6:
        return min(1.0, similarity)
    if similarity >= 0.8:
        return min(1.0, similarity * 0.8)
    return 0.0


def _shape(name: str) -> str:
    """Collapse digit runs so numbered variants share a shape."""
    result: list[str] = []
    previous_digit = False
    for char in name:
        is_digit = char.isdigit()
        if is_digit and previous_digit:
            continue
        result.append("#" if is_digit else char)
        previous_digit = is_digit
    return "".join(result)


def burst_detected(timestamps: list[float], limit: int, window: float) -> bool:
    """True when `limit` events fall inside `window` seconds."""
    if len(timestamps) < limit:
        return False
    ordered = sorted(timestamps)
    for index in range(len(ordered) - limit + 1):
        if ordered[index + limit - 1] - ordered[index] <= window:
            return True
    return False


def scaled_penalty(score: float, base: float = 1.0) -> float:
    """Penalty multiplier used by SmartMod, capped so one bug cannot nuke a server."""
    return max(0.0, min(3.0, base * (1.0 + math.log1p(max(0.0, score)) / 3.0)))


def snapshot_of(verdict: Verdict) -> dict[str, Any]:
    """Serialisable summary for the security log."""
    return {
        "action": verdict.action,
        "reason": verdict.reason,
        "risk_score": round(verdict.risk_score, 1),
        "count": verdict.count,
        "limit": verdict.limit,
        "burst": verdict.burst_count,
        "severity": verdict.severity,
    }