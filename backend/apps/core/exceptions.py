# apps/core/exceptions.py
"""Domain exceptions mapped to machine-readable API error codes.

The frontend keys behavior off `code` (see docs/FRONTEND_CONTRACT.md).
"""
from rest_framework import status
from rest_framework.exceptions import APIException


class JOneAPIError(APIException):
    status_code = status.HTTP_400_BAD_REQUEST
    default_detail = "The request could not be completed."
    default_code = "ERROR"


class InvalidDatesError(JOneAPIError):
    default_detail = "The selected dates are invalid."
    default_code = "INVALID_DATES"


class CapacityExceededError(JOneAPIError):
    default_detail = "The guest or room count exceeds allowed capacity."
    default_code = "CAPACITY_EXCEEDED"


class RoomUnavailableError(JOneAPIError):
    status_code = status.HTTP_409_CONFLICT
    default_detail = "No rooms of this type are available for the selected dates."
    default_code = "ROOM_UNAVAILABLE"


class BookingExpiredError(JOneAPIError):
    status_code = status.HTTP_409_CONFLICT
    default_detail = "This booking has expired. Please start a new booking."
    default_code = "BOOKING_EXPIRED"


class BookingStateError(JOneAPIError):
    status_code = status.HTTP_409_CONFLICT
    default_detail = "The booking is not in a state that allows this action."
    default_code = "INVALID_BOOKING_STATE"


class CancellationNotAllowedError(JOneAPIError):
    default_detail = "This booking can no longer be cancelled."
    default_code = "CANCELLATION_NOT_ALLOWED"


class OfferNotApplicableError(JOneAPIError):
    default_detail = "This offer cannot be applied to the selected stay."
    default_code = "OFFER_NOT_APPLICABLE"


# An offer is OPTIONAL: rejecting one must never fail the whole quote. Each
# subclass carries its own code so the booking form can show the guest the
# specific reason on the offer field and re-price the stay without the code.
class OfferCodeInvalidError(OfferNotApplicableError):
    default_detail = "This offer code is not valid."
    default_code = "OFFER_CODE_INVALID"


class OfferExpiredError(OfferNotApplicableError):
    default_detail = "This offer has expired."
    default_code = "OFFER_EXPIRED"


class OfferNotStartedError(OfferNotApplicableError):
    default_detail = "This offer has not started yet."
    default_code = "OFFER_NOT_STARTED"


class OfferDoesNotCoverStayError(OfferNotApplicableError):
    default_detail = "This offer does not cover the whole of your stay."
    default_code = "OFFER_DOES_NOT_COVER_STAY"


class OfferStayTooShortError(OfferNotApplicableError):
    default_detail = "Your stay is shorter than this offer's minimum."
    default_code = "OFFER_STAY_TOO_SHORT"


class OfferStayTooLongError(OfferNotApplicableError):
    default_detail = "Your stay is longer than this offer's maximum."
    default_code = "OFFER_STAY_TOO_LONG"


class OfferRoomTypeNotEligibleError(OfferNotApplicableError):
    default_detail = "This offer does not apply to the selected room type."
    default_code = "OFFER_ROOM_TYPE_NOT_ELIGIBLE"


class PaymentError(JOneAPIError):
    default_detail = "The payment could not be completed."
    default_code = "PAYMENT_FAILED"


class PaymentAlreadyCompletedError(JOneAPIError):
    status_code = status.HTTP_409_CONFLICT
    default_detail = "This booking is already fully paid."
    default_code = "PAYMENT_ALREADY_COMPLETED"


class PaymentAmountMismatchError(JOneAPIError):
    default_detail = "The verified payment amount does not match the expected amount."
    default_code = "PAYMENT_AMOUNT_MISMATCH"


class PaymentNotConfiguredError(JOneAPIError):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    default_detail = "Online payments are not configured. Please contact the hotel."
    default_code = "PAYMENT_NOT_CONFIGURED"


class PaymentGatewayError(JOneAPIError):
    status_code = status.HTTP_502_BAD_GATEWAY
    default_detail = "The payment gateway could not be reached. Please try again."
    default_code = "PAYMENT_GATEWAY_ERROR"


class OutstandingBalanceError(JOneAPIError):
    default_detail = "The booking has an outstanding balance."
    default_code = "OUTSTANDING_BALANCE"
