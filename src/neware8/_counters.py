"""Step, cycle and test quantities accumulated from recorded device counters."""

from fractions import Fraction
from .measurements import current_scale
from .messages import HistoryRecord


COUNTERS = {
    "charging_capacity_ah": "charge_capacity_raw",
    "discharging_capacity_ah": "discharge_capacity_raw",
    "charging_energy_wh": "charge_energy_raw",
    "discharging_energy_wh": "discharge_energy_raw",
}
COLUMNS = tuple(
    prefix + name
    for prefix in ("step_", "cycle_", "")
    for name in (
        *COUNTERS,
        "cumulative_capacity_ah",
        "net_capacity_ah",
        "cumulative_energy_wh",
        "net_energy_wh",
    )
)


class CounterTotals:
    """Accumulate step endpoints without integrating sampled current or voltage."""

    def __init__(self, charge_range: int | None) -> None:
        self.charge_range = charge_range
        self.previous_sequence = 0
        self.previous_occurrence: tuple[int, int, int] | None = None
        self.previous: dict[str, Fraction | None] = dict.fromkeys(COUNTERS)
        self.test_base: dict[str, Fraction | None] = dict.fromkeys(COUNTERS, Fraction(0))
        self.cycle_base: dict[str, Fraction | None] = dict.fromkeys(COUNTERS, Fraction(0))
        self.invalid: set[str] = set()

    @staticmethod
    def _add(a: Fraction | None, b: Fraction | None) -> Fraction | None:
        return a + b if a is not None and b is not None else None

    def update(self, record: HistoryRecord) -> dict[str, float | None]:
        occurrence = (record.cycle, record.step_id, record.step_change_count)
        contiguous = record.sequence == self.previous_sequence + 1
        new_step = occurrence != self.previous_occurrence
        if new_step:
            if self.previous_occurrence is not None:
                for name in COUNTERS:
                    self.test_base[name] = self._add(self.test_base[name], self.previous[name])
                    self.cycle_base[name] = self._add(self.cycle_base[name], self.previous[name])
                if occurrence[0] != self.previous_occurrence[0]:
                    self.cycle_base = dict.fromkeys(COUNTERS, Fraction(0))
            self.invalid.clear()
        if not contiguous:
            self.test_base = dict.fromkeys(COUNTERS)
            self.cycle_base = dict.fromkeys(COUNTERS)

        raw_range = record.range_raw
        if raw_range == 0:
            if self.charge_range is None:
                raise ValueError("zero counter range requires explicit channel range")
            raw_range = self.charge_range
        divisor = current_scale(raw_range) * 3_600_000
        current: dict[str, Fraction | None] = {}
        for name, raw_name in COUNTERS.items():
            value = Fraction(getattr(record, raw_name), divisor)
            previous = self.previous[name]
            if value < 0 or (not new_step and previous is not None and value < previous):
                self.invalid.add(name)
            current[name] = None if name in self.invalid else value

        values: dict[str, Fraction | None] = {}
        for prefix, base in (("step_", None), ("cycle_", self.cycle_base), ("", self.test_base)):
            for name in COUNTERS:
                values[prefix + name] = (
                    current[name] if base is None else self._add(base[name], current[name])
                )
            for quantity, unit in (("capacity", "ah"), ("energy", "wh")):
                charge = values[f"{prefix}charging_{quantity}_{unit}"]
                discharge = values[f"{prefix}discharging_{quantity}_{unit}"]
                values[f"{prefix}cumulative_{quantity}_{unit}"] = self._add(charge, discharge)
                values[f"{prefix}net_{quantity}_{unit}"] = (
                    charge - discharge if charge is not None and discharge is not None else None
                )
        self.previous = current
        self.previous_occurrence = occurrence
        self.previous_sequence = record.sequence
        return {name: float(value) if value is not None else None for name, value in values.items()}
