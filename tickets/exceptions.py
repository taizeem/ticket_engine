class SeatAllocationError(Exception):
    """Base error for allocation failures."""
    pass


class InsufficientSeatsAvailable(SeatAllocationError):
    """Raised when the requested number of unreserved seats is unavailable."""
    pass


class EventNotActive(SeatAllocationError):
    """Raised when attempting to allocate seats for an inactive or invalid event."""
    pass



class ReservationExpiredError(SeatAllocationError):
    """Raised when attempting to confirm a hold whose TTL has already expired."""
    pass


class ReservationAlreadyProcessedError(SeatAllocationError):
    """Raised when confirming a reservation that is no longer in PENDING state."""
    pass


class InvalidPaymentAmountError(SeatAllocationError):
    """Raised when the webhook payment amount does not match total seat prices."""
    pass