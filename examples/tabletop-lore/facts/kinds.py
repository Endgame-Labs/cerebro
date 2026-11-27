"""Fact kinds for The Salt Crown tabletop campaign wiki.

Each class defines a fact kind that Factoid assigns during extraction. Context agents filter on them by snake-case
name (for example, `classes: [oath, betrayal]` or `classes: [rumor, secret, item_transfer]`).
"""

from factoid import Fact


class Oath(Fact):
    """A sworn vow, pact, treaty, charter pledge, or binding promise made by a character or faction."""

    guidance = (
        "Write each sworn oath, charter pledge, or binding promise as its own fact, naming who swore it, to whom, "
        "and what it obligates them to do."
    )


class Betrayal(Fact):
    """A broken oath, double-cross, treasonous act, or covert sabotage against an ally, faction, or charter."""

    guidance = (
        "Write each broken oath, double-cross, or act of sabotage as its own fact, naming who committed it, who was "
        "betrayed, and what happened."
    )


class Rumor(Fact):
    """An unverified tavern whisper, dockside gossip, street speculation, or unconfirmed claim."""

    guidance = (
        "Write each unverified rumor, gossip, or hearsay claim as its own fact, stating that it is a rumor or "
        "unconfirmed claim and naming who spread it or where it was heard when the text says."
    )


class Secret(Fact):
    """A hidden identity clue, concealed allegiance, secret meeting, or covert knowledge kept from other factions."""

    guidance = (
        "Write each hidden clue, concealed motive, secret meeting, or suspected cover identity as its own fact, "
        "keeping the exact evidence without overstating certainty."
    )


class ItemTransfer(Fact):
    """The handing over, smuggling, theft, hiding, seizure, or recovery of an important item, writ, key, or artifact."""

    guidance = (
        "Write each transfer, theft, hiding, or custody change of an important item or document as its own fact, "
        "naming the item, who gave or lost it, and who holds it."
    )


class Other(Fact):
    """Any established campaign fact that does not fit the other kinds, such as roles, locations, dates, faction
    standings, imprisonments, or public votes."""
