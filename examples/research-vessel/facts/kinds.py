"""Fact kinds for Expedition SW-26-09 aboard R/V Shearwater.

Each class name becomes a snake_case kind name for agent filters:
CalibrationDrift -> calibration_drift, EquipmentFault -> equipment_fault,
SafetyLimit -> safety_limit, SamplingChange -> sampling_change, Other -> other.
"""

from factoid import Fact


class CalibrationDrift(Fact):
    """A sensor offset, calibration bias, salinity or depth drift, battery scale error, or calibration check result,
    including corrections that resolve an earlier drift or miscalibration."""

    guidance = ("Write each sensor drift, offset, calibration check, or recalibration correction as a fact of its own, "
                "keeping the exact instrument name, numeric value, unit, and date.")


class EquipmentFault(Fact):
    """A mechanical, electrical, hydraulic, seal, or sensor defect, damage, tag-out, grounding, or hardware warning
    affecting a scientific instrument or deck machine."""

    guidance = ("Write each hardware defect, nicked seal, crack, fault, or out-of-service tag-out as a fact of its "
                "own, naming the instrument, the affected component, and who observed or tagged it.")


class SafetyLimit(Fact):
    """An operational safety threshold, safe working load, calibration tolerance, battery cutoff voltage, or weather
    launch limit, and any event that approaches or exceeds it."""

    guidance = ("Write each safety limit, safe working load, cutoff voltage, calibration tolerance, or weather limit "
                "as a fact of its own, with the exact numeric limit and whether an event exceeded it.")


class SamplingChange(Fact):
    """A schedule shift, cast deferral, aborted haul, early instrument recovery, weather routing change, or
    disagreement over the station sampling plan."""

    guidance = ("Write each change to a station cast date, recovery schedule, weather routing cutoff, or watch "
                "disagreement over operations as a fact of its own, keeping the dates and who decided or disagreed.")


class Other(Fact):
    """An operational log entry, deployment, completed cast, or voyage fact that fits none of the other kinds."""
