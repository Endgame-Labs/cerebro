"""The kinds of fact this wet-lab workspace extracts, one class each.

A kind's name is its class name in snake case: `ProtocolDeviation` is `protocol_deviation`, which an agent file
filters on as `classes: [protocol_deviation]`. The docstring tells the model how to classify a fact, and `guidance`
steers extraction from bench notes, failed-run write-ups, and meeting minutes.
"""

from factoid import Fact


class ProtocolDeviation(Fact):
    """A change to a written bench protocol, a step modification, or a deviation run at the bench, such as an altered
    temperature, incubation time, antibiotic concentration, buffer recipe, or version update."""

    guidance = (
        "Write each protocol change, parameter update, or bench deviation as a fact of its own, keeping exact "
        "temperatures, durations, concentrations, version numbers, and who made or ran the change."
    )


class Measurement(Fact):
    """A quantitative experimental observation or assay readout, such as a Ct value, nucleic acid or protein yield,
    OD600 reading, colony count, band intensity, or replicate spread."""

    guidance = (
        "Keep every measured number, unit, replicate count, reagent lot, strain, and protocol version in the fact "
        "that holds the result."
    )


class FailureMode(Fact):
    """A failed bench run, confirmed reagent defect, precipitate, false positive, contamination, low yield, or
    recurring pitfall, along with its confirmed cause or quarantine notice."""

    guidance = (
        "Write each bench failure, bad reagent lot symptom, or confirmed pitfall as a fact of its own, naming the "
        "protocol, reagent lot, or strain affected and what went wrong."
    )


class Hypothesis(Fact):
    """An unconfirmed explanation for an experimental result or failure, an active disagreement between lab members
    about a root cause or parameter, or a troubleshooting test to try next."""

    guidance = (
        "Write each proposed explanation, disputed theory, or open troubleshooting question as a fact, naming who "
        "proposed or disagreed with it and distinguishing it from a confirmed cause."
    )


class Decision(Fact):
    """A lab decision, protocol version release, reagent quarantine, vendor switch, or onboarding schedule
    commitment agreed by the PI or lab team."""

    guidance = (
        "Write each formal lab decision, quarantine, protocol approval, or schedule change as a fact, naming who "
        "decided it and when it takes effect."
    )


class Other(Fact):
    """A fact that fits none of the other kinds, such as routine inventory details, roles, or general lab context."""
