"""The kinds of fact this workspace extracts from postmortems, handoffs, and runbooks.

A kind's name is its class name in snake case: `RootCause` is `root_cause`, the
name an agent file filters on with `classes:`.
"""

from factoid import Fact


class RootCause(Fact):
    """An underlying technical cause, faulty setting, or architectural flaw that triggered or prolonged an incident or
    recurring alert, as well as a hypothesis that later evidence disproved."""

    guidance = ("Write each technical root cause, contributing factor, or disproved hypothesis as a fact of its own, "
                "naming the service, incident, component, and specific setting or job involved.")


class Mitigation(Fact):
    """A deployed fix, rollback, failover, workaround, or operational safeguard taken to stop or reduce impact."""

    guidance = ("Write each mitigation, workaround, rollback, or safeguard as a fact of its own, stating who took it, "
                "when, what release SHA or config it used, and whether it is temporary or permanent.")


class Threshold(Fact):
    """A numeric SLO target, error-budget limit, alert firing rule, timeout, concurrency cap, retry bound, or circuit
    breaker setting."""

    guidance = ("Keep every exact numeric threshold, SLO target, error-budget percentage, timeout, TTL, cap, and "
                "alert condition in the facts that hold them, naming the service and parameter.")


class ActionItem(Fact):
    """A concrete follow-up engineering task, ticket, migration, or decision with an owner or target date."""

    guidance = ("Write each follow-up task, ticket ID (such as OPS-412), owner, status, and target or rescheduled "
                "date as a fact of its own.")


class IncidentEvent(Fact):
    """An incident impact measurement, timeline event, alert firing, disagreement, architectural decision, or corrected
    impact count."""

    guidance = ("Write each incident's severity, duration, service impact, corrected count, or engineering decision as "
                "a fact of its own, preserving exact dates, numbers, and names.")


class Other(Fact):
    """A fact that fits none of the other kinds."""
