"""Errors exposed by the native client."""


class NewareError(Exception):
    """Base class for client failures."""


class ProtocolError(NewareError, ValueError):
    """Malformed traffic or an unexpected exchange."""


class ConnectionLost(NewareError):
    """The controller connection is unavailable."""


class OperationUncertain(NewareError):
    """A write may have reached the controller; it must not be replayed."""


class OperationRejected(NewareError):
    """The controller explicitly rejected an operation."""


class StateError(NewareError):
    """The requested operation is incompatible with the observed state."""


class StorageError(NewareError):
    """Data could not be durably recorded or interpreted for export."""
