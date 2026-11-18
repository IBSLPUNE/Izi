# -*- coding: utf-8 -*-
"""Courier error types. Callers decide what to show the user and what is safe to retry."""


class CourierError(Exception):
    def __init__(self, message, *, code="", http_status=None, payload=None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.http_status = http_status
        self.payload = payload


class CourierConfigError(CourierError):
    """Settings are missing or wrong. Nothing was sent."""


class CourierValidationError(CourierError):
    """Our own checks rejected the input. Nothing was sent."""


class CourierAuthError(CourierError):
    """Login failed or the token was refused. Nothing was processed."""


class CourierBusinessError(CourierError):
    """Blue Dart received the request and refused it (IsError = true)."""


class CourierTechnicalError(CourierError):
    """Blue Dart or the network failed. Safe to retry for reads and idempotent calls only."""


class CourierOutcomeUnknown(CourierError):
    """A waybill creation timed out or failed midway. It may already exist. Do not retry until it is checked."""