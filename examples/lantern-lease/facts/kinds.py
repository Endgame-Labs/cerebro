from factoid import Fact


class Obligation(Fact):
    """A duty, right, or allocation of cost stated in a document, with its document and execution status."""


class Deadline(Fact):
    """A notice date, performance date, or planned date, with who states it and whether it is agreed."""


class DisputedClaim(Fact):
    """An attributed assertion that another participant challenges or that remains unverified."""


class DocumentState(Fact):
    """A document's identity, signatures, amendment relationship, or correction to a register entry."""


class ClientInstruction(Fact):
    """A client's decision, spending limit, or instruction to counsel, rather than a contract term."""


class Other(Fact):
    """A fact that fits none of the other kinds."""
