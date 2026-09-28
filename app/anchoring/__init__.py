"""Anchoring providers and the factory that selects one."""

from __future__ import annotations

from .auto import AutoProvider
from .base import (
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_PREPARED,
    STATUS_SIMULATED,
    STATUS_SUBMITTED,
    AnchorProvider,
    AnchorReceipt,
)
from .ethereum import ANCHOR_CONTRACT_SOURCE, EthereumProvider, encode_anchor_calldata
from .opentimestamps import OpenTimestampsProvider
from .simulated import SimulatedProvider

PROVIDERS = {
    "auto": AutoProvider,
    "simulated": SimulatedProvider,
    "opentimestamps": OpenTimestampsProvider,
    "ots": OpenTimestampsProvider,
    "ethereum": EthereumProvider,
    "evm": EthereumProvider,
}


def build_provider(config) -> AnchorProvider:
    """Return the provider named in the configuration.

    An unknown name (or a provider that reports itself unavailable) falls back to
    ``simulated`` so an anchor can always be produced -- every receipt carries a
    ``status``, so a fallback is never silently passed off as a real anchor.
    """
    requested = (config.anchoring.provider or "auto").strip().lower()
    provider_class = PROVIDERS.get(requested)

    if provider_class is None:
        return AutoProvider(config)

    provider = provider_class(config)
    ok, _reason = provider.available()
    if not ok and requested not in {"simulated", "auto"}:
        return SimulatedProvider(config)
    return provider


def provider_catalogue(config) -> list[dict]:
    """Describe every provider -- drives the Anchoring settings screen."""
    aliases = {
        "auto": {"auto"},
        "opentimestamps": {"opentimestamps", "ots"},
        "ethereum": {"ethereum", "evm"},
        "simulated": {"simulated"},
    }
    selected = (config.anchoring.provider or "auto").strip().lower()

    catalogue = []
    for name in ("auto", "opentimestamps", "ethereum", "simulated"):
        provider = PROVIDERS[name](config)
        info = provider.describe()
        info["selected"] = selected in aliases[name]
        catalogue.append(info)
    return catalogue


__all__ = [
    "AnchorProvider",
    "AnchorReceipt",
    "OpenTimestampsProvider",
    "EthereumProvider",
    "SimulatedProvider",
    "AutoProvider",
    "build_provider",
    "provider_catalogue",
    "encode_anchor_calldata",
    "ANCHOR_CONTRACT_SOURCE",
    "PROVIDERS",
    "STATUS_SUBMITTED",
    "STATUS_PENDING",
    "STATUS_PREPARED",
    "STATUS_SIMULATED",
    "STATUS_FAILED",
]
