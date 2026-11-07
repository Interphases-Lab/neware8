"""Channel profiles derived from the controller's own metadata (SendInfo). No I/O.

The manufacturer client takes a channel's ranges unchanged from SendInfo (code-derived):
the voltage range is in V, the charge range gives the rated current
(`Unit.GetCurrRange`) and the program's current scale (`Unit.GetCurrScale`).

The physical ID is the channel's index in address order: unit 1 channel 1 is 0, and every
advertised main channel counts. This is the manufacturer's default channel map; all 80
entries of our site's map follow it (observed). A site that changed its map in the
manufacturer software must configure the changed channels.
"""

from .measurements import current_range_a, current_scale
from .messages import DeviceInfo
from .models import ChannelAddress, ChannelProfile


def derive_profiles(info: DeviceInfo) -> tuple[dict[ChannelAddress, ChannelProfile], list[str]]:
    """Profiles of the advertised main channels, and why any channel has none."""
    channels = info.main_channels()
    profiles: dict[ChannelAddress, ChannelProfile] = {}
    problems: list[str] = []
    for physical_id, key in enumerate(sorted(channels)):
        block, address = channels[key], ChannelAddress(*key)
        try:
            profiles[address] = ChannelProfile(
                address,
                physical_id,
                voltage_rating_v=float(block.voltage_range),
                current_rating_a=current_range_a(block.charge_range),
                current_scale=current_scale(block.charge_range),
                charge_range_raw=block.charge_range,
            )
        except ValueError as error:
            problems.append(f"channel {key[0]}-{key[1]}: {error}")
    return profiles, problems
