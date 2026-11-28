from factoid import Fact


class AttributedClaim(Fact):
    """A source's assertion or observation, which does not establish independent truth."""

    guidance = "Name the speaker in full, preserve their role, and distinguish a measurement from an interpretation."


class Correction(Fact):
    """A correction, retraction, or clarification of an earlier statement."""

    guidance = "Keep who corrected what, the replacement statement, and any claim explicitly left standing."


class Commitment(Fact):
    """An undertaking or conditional plan, not proof of completion."""

    guidance = "Name the owner, the promised action, its date, and any stated condition."
