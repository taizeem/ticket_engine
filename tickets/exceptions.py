class SeatAllocationError(Exception):
    """Base error for allocation failures."""
    pass


class InsufficientSeatsAvailable(SeatAllocationError):
    """Raised when the requested number of unreserved seats is unavailable."""
    pass


class EventNotActive(SeatAllocationError):
    """Raised when attempting to allocate seats for an inactive or invalid event."""
    pass