"""The kinds of fact a clinic's records give, one class each. Every fact takes one of them.

The docstring is what the model reads to give a fact its kind, and `guidance` is what it reads when it pulls facts
out of a record. An agent file filters on a kind by its name in snake case, as `classes: [owner_concern]`.
"""

from factoid import Fact


class Diagnosis(Fact):
    """A diagnosis, assessment, or problem a clinician names for a patient, such as diabetes mellitus or a splenic
    mass, including a rule out, a change in status such as improving or resolved, and a condition the patient has
    had for a long time."""

    guidance = ("Write each diagnosis or assessment as a fact of its own, naming the patient, the clinician who made "
                "it, and its status, such as suspected, improving, stable, or resolved.")


class Medication(Fact):
    """A drug, diet, or other treatment a patient is given, started, changed, stopped, or held, with its dose, route,
    and frequency, or a correction to a dose written earlier."""

    guidance = ("Write each medication as a fact of its own with the drug, the dose and its units, the route, and how "
                "often, exactly as the record gives them. When a record corrects or changes an earlier dose, write "
                "the new dose and say what it replaces.")


class Allergy(Fact):
    """A drug, food, or other substance a patient reacted to or must not be given, or a record that the patient has
    no known allergy."""

    guidance = "Write each allergy as a fact with the substance, the reaction, and when it happened, if the text says."


class OwnerConcern(Fact):
    """Something an owner worries about, asks for, cannot do, or disagrees on: cost, giving medication, handling at
    home, the household, or the choice between treatments."""

    guidance = ("Write each concern or request an owner raises as a fact of its own, naming the owner. When two owners "
                "want different things, write what each one wants.")


class Finding(Fact):
    """What an exam, a test, or a lab result found: a weight, a vital sign, a lab value with its flag, an image, or
    how the patient behaved in the clinic."""

    guidance = "Keep each value with its units, its reference range or flag, and the date, as the record gives them."


class Plan(Fact):
    """A next step for a patient: a recheck, a test, a procedure, or a referral that is booked, moved, or cancelled,
    and an instruction an owner was given, such as signs that mean go to an emergency clinic."""

    guidance = "Write each next step as a fact with its date, and when a date moves or a booking is cancelled, say so."


class Other(Fact):
    """A fact that fits none of the other kinds, such as clinic hours, equipment, or staffing."""
