"""The kinds of fact this workspace gives, one class each. Each fact takes one of them.

A kind's name is its class name in snake case: `RiskObjection` is `risk_objection`, the name an agent file filters
on, as `classes: risk_objection`. The docstring is what the model reads to give a fact its kind, and `guidance` is
what it reads when it pulls facts out of a record. Add a class to add a kind. A fact keeps the kind it was given: a
kind added or renamed here reaches a record when it is extracted again.
"""

from factoid import Fact


class RiskObjection(Fact):
    """An expressed concern, objection, blocker, uncertainty, or risk to an outcome, such as a deal closing,
    renewing, or expanding."""

    guidance = ("Write each concern, objection, blocker, or risk a person raises as a fact of its own, saying who "
                "raised it and what it puts at risk.")


class Goal(Fact):
    """A customer's stated goal, desired outcome, or initiative."""

    guidance = "Write each goal or desired outcome a customer states as a fact, naming whose goal it is."


class NeedPain(Fact):
    """A desired outcome, unmet need, requirement, or problem a customer experiences today, such as time lost to a
    manual task. A problem the customer lives with is a need, not a risk to the deal."""

    guidance = "Write each need or problem a customer describes as a fact of its own, with what it costs them."


class UseCase(Fact):
    """How a customer uses or intends to use the product."""

    guidance = "Write how a customer uses, or means to use, the product as a fact of its own."


class NextStep(Fact):
    """An agreed next step between the seller and the customer."""

    guidance = "Write each next step the two sides agree on as a fact, with who takes it and when, if the text says."


class ActionItem(Fact):
    """A concrete assigned, requested, promised, or agreed task to perform. A completed past task alone is not an
    outstanding action item."""

    guidance = "Write each task someone is asked, assigned, or promises to do as a fact, naming who owns it."


class Timeline(Fact):
    """A date, deadline, duration, sequence, or schedule for an event or activity."""

    guidance = "Keep the dates, deadlines, and durations the text gives in the facts that hold them."


class Identity(Fact):
    """An entity's name, identifier, contact information, role, or intrinsic attribute, such as a person's title."""

    guidance = "Write a person's role or title, or a company's defining attribute, as a fact when the text gives it."


class Relationship(Fact):
    """A relationship between people, organizations, or products, such as employment, reporting, ownership, or
    use."""

    guidance = "Write who works for, reports to, owns, or uses whom or what as a fact of its own."


class DecisionCommitment(Fact):
    """An explicit decision, approval, rejection, agreed action, promise, or commitment."""

    guidance = "Write each decision, approval, rejection, or commitment as a fact, naming who made it."


class CommercialTerms(Fact):
    """Price, budget, payment, contract, procurement terms, or quantified commercial value."""

    guidance = "Keep the amounts, seats, and terms the text gives in the facts that hold them."


class Other(Fact):
    """A fact that fits none of the other kinds."""
