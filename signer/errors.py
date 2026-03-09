class SignerError(Exception):
    """Base signer exception."""


class AmbiguousBroadcastError(SignerError):
    """Unknown broadcast state; funds must remain reserved for reconciliation."""


class DeterministicSigningError(SignerError):
    """Deterministic pre-broadcast/signing failure safe to mark failed."""


class SignerConfigurationError(DeterministicSigningError):
    """Signer misconfiguration detected."""


class RetryableSignerError(AmbiguousBroadcastError):
    """Transient signer/provider failure that should be retried with reconciliation safeguards."""
