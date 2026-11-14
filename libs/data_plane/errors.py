"""Errors shared by the data-plane libraries.

Every error is a plain exception so the libraries stay stdlib-only and portable
across the local and external planes.
"""

from __future__ import annotations


class DataPlaneError(Exception):
    """Base class for every data-plane failure."""


class ContractError(DataPlaneError):
    """A source, capability, or recovery-group contract is invalid."""


class ValidationError(DataPlaneError):
    """An input record or manifest does not match the declared schema."""


class AuthorizationError(DataPlaneError):
    """An identity is not allowed to perform the requested operation."""


class ConfirmationRequired(DataPlaneError):
    """The operation is destructive, bulk, irreversible, or cross-domain."""


class ConfirmationError(DataPlaneError):
    """A confirmation token is missing, expired, reused, or does not match."""


class IdempotencyConflict(DataPlaneError):
    """The same idempotency key was reused with a different payload."""


class EpochError(DataPlaneError):
    """A log or source epoch changed; the consumer must reconcile."""


class CursorError(DataPlaneError):
    """A cursor would move backwards or skip a gap."""


class StaleError(DataPlaneError):
    """A source or consumer is stale and must be fully reconciled."""


class PublishError(DataPlaneError):
    """A staging generation cannot be published atomically."""


class ManifestError(DataPlaneError):
    """A backup generation manifest is incomplete or incompatible."""


class OutcomeUnknown(DataPlaneError):
    """An effect may or may not have landed; never replay blindly."""


class RestoreError(DataPlaneError):
    """A restore plan cannot be selected or verified."""
