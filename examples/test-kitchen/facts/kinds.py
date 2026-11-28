"""The kinds of fact this kitchen's wiki gives, one class each. Each fact takes one of them.

The docstring is what the model reads to give a fact its kind, and `guidance` is what it reads when it pulls facts
out of a record. An agent filters on a kind by its snake case name, as `classes: [price_change]`.
"""

from factoid import Fact


class Allergen(Fact):
    """An allergen a dish, a component or an ingredient holds, or is free of, such as nuts, sesame, dairy, egg,
    wheat, soy, fish or shellfish."""

    guidance = ("Write each allergen a dish or component holds, or is said to be free of, as a fact of its own, "
                "naming the dish and the ingredient it comes from.")


class Substitution(Fact):
    """A swap the kitchen makes in a dish for an allergy, a diet or a shortage, or an ingredient replaced for
    good."""

    guidance = ("Write each substitution as a fact that says what replaces what, in which dish, and for whom or "
                "why.")


class PriceChange(Fact):
    """A price that moved: what a supplier charges for an item, or what the menu charges for a dish, with the old
    and new amounts when the text gives them."""

    guidance = ("Write each price, and each change of price, as a fact with the item, the amount per unit, the "
                "supplier or the menu, and the date it took effect.")


class Other(Fact):
    """A fact that fits none of the other kinds: a method, a station, a date, a decision, a temperature."""
