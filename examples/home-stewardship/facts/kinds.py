"""The kinds of fact this home-stewardship workspace extracts, one class each.

A kind's name is its class name in snake case: `ContractorQuote` is `contractor_quote`,
which agent files filter on with `classes: [contractor_quote]`.
"""

from factoid import Fact


class Quirk(Fact):
    """A non-obvious physical quirk, fragile valve, hidden shutoff, wiring surprise,
    or house-specific warning that anyone working on the house or garden must know."""

    guidance = (
        "Write each physical quirk, warning, fragile valve, buried shutoff, or "
        "do-not-touch rule as a fact of its own, naming the exact component, valve "
        "or circuit number, and what goes wrong if ignored."
    )


class Specification(Fact):
    """A model number, serial number, capacity, paint code, hardware dimension,
    breaker assignment, plant cultivar, or material specification."""

    guidance = (
        "Keep exact model numbers, serial numbers, paint codes, dimensions, "
        "breaker numbers, pressure settings, and material specs in their own facts."
    )


class Warranty(Fact):
    """A warranty duration, expiration date, registration number, coverage condition,
    or workmanship guarantee for a home system, appliance, or contractor job."""

    guidance = (
        "Write each warranty term, expiration date, coverage limit, or condition "
        "that voids coverage as a fact, naming the covered system or job."
    )


class ContractorQuote(Fact):
    """A contractor's price estimate, scope of work, schedule, payment terms,
    or revision to an earlier quote."""

    guidance = (
        "Write each contractor quote, scope, dollar amount, schedule, or price "
        "revision as a fact, naming the contractor and the system or job."
    )


class SeasonalTask(Fact):
    """A recurring or upcoming maintenance chore, inspection, spray, pruning,
    winterization step, or scheduled contractor visit with its due date or season."""

    guidance = (
        "Write each upcoming or seasonal maintenance task as a fact with its due "
        "date, seasonal window, and any weather or sequence constraint."
    )


class Other(Fact):
    """A concrete fact about the house, systems, contractors, or garden that fits
    none of the other kinds."""
