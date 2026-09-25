from __future__ import annotations

"""Shared application errors."""


class QanerisError(ValueError):
    code = "qaneris_error"
    status_code = 400

    def __init__(self, message: str):
        self.message = message
        super().__init__(f"{self.code}: {message}")


class DatasourceNotFoundError(QanerisError):
    code = "datasource_not_found"
    status_code = 404


class DatasourceUnavailableError(QanerisError):
    code = "datasource_unavailable"


class DatasourceConnectionTestError(QanerisError):
    """A candidate connection could not be established.

    Raised at the connection-test boundary. Every adapter/driver exception is converted into this
    one Qaneris error there and sanitized on the way in, so a driver's raw exception type and
    message - which can quote a host, a username or a connection string - never reach the product
    layer. A failed test is what keeps a candidate profile from being persisted: the caller learns
    the connection did not work, not what the server said about it.
    """

    code = "datasource_connection_test_failed"


class DatasourceSecureProfileRequiredError(QanerisError):
    """A secure-only operation was asked to act on a datasource without a secure connection profile.

    A legacy ``DatasourceCreate`` datasource stores a raw connection document, not a
    ``connection_profile_v1`` document, so it has no ``SecretReference`` to rotate and no
    candidate to test. Legacy datasources are never migrated onto the secure path implicitly.
    """

    code = "datasource_secure_profile_required"
    status_code = 409


class DatasourceUpdateError(QanerisError):
    """A datasource connection could not be switched to the candidate profile."""

    code = "datasource_update_failed"


class DatasourceDeleteError(QanerisError):
    """A datasource and its published graph could not be removed together."""

    code = "datasource_delete_failed"


class DatasourceNotReadyError(QanerisError):
    code = "datasource_not_ready"


class GraphUnavailableError(QanerisError):
    """The enterprise data graph could not be read.

    Semantic retrieval fails closed on this error instead of reporting "no candidates", because an
    unreachable graph and a graph without matches are different outcomes.
    """

    code = "graph_unavailable"
    status_code = 503


class QueryPlanningError(QanerisError):
    code = "query_planning_failed"


class QueryContextBuildError(QueryPlanningError):
    """A planner-facing query context cannot be built from this grounding.

    Raised when the grounding is not executable (unresolved ambiguity, unbound slot, cross-source
    binding) or when the query asks for something Phase 1 planning cannot express. It fails closed:
    an incomplete context is never handed to the planner, and the planner never repairs it.
    """

    code = "query_context_build_failed"


class IntentParsingError(QanerisError):
    code = "intent_parsing_failed"


class ModelInvocationError(QanerisError):
    """The configured model could not be reached, or the provider refused the request.

    Raised at the model gateway boundary. Provider and transport failures — authentication,
    rate limit, timeout, network, unusable provider response — are converted into this one
    Qaneris error there, so no provider SDK exception type reaches the semantic, querying or
    interface layers. A model that answers with invalid JSON is a different outcome and stays
    ``IntentParsingError``.
    """

    code = "model_invocation_failed"
    status_code = 503


class QuerySafetyError(QanerisError):
    code = "unsafe_query"


class ExecutionValidationError(QanerisError):
    code = "execution_validation_failed"


class QueryExecutionError(QanerisError):
    code = "execution_failed"


class CredentialStoreConfigurationError(QanerisError):
    """The managed credential store was asked to work without a usable configuration.

    Raised for a missing ``QANERIS_SECRET_STORE_DIR`` / ``QANERIS_MASTER_KEY``, for a store
    directory inside the repository, and for a master key that is not 32 bytes of URL-safe Base64.
    It fails closed on purpose: a key is never generated on the fly and a weak default is never
    substituted.
    """

    code = "credential_store_configuration_error"


class ManagedSecretNotFoundError(QanerisError):
    """No managed secret is stored under this identifier.

    Also raised for an identifier that is not a well-formed ``sec_<uuid4 hex>``: such a value can
    never name a stored secret, and answering the same way keeps the error from becoming an oracle
    for probing the store directory.
    """

    code = "managed_secret_not_found"
    status_code = 404


class ManagedSecretInUseError(QanerisError):
    """A referenced secret cannot be deleted while a stored connection profile still names it."""

    code = "managed_secret_in_use"
    status_code = 409


class ManagedSecretIntegrityError(QanerisError):
    """A stored secret could not be read back as a complete, authentic document.

    Raised for an unreadable or malformed document, a wrong algorithm or version tag, a bad nonce
    or ciphertext, and for an authentication tag that does not verify - which is also what a wrong
    master key produces. It never returns a partially parsed result.
    """

    code = "managed_secret_integrity_failed"


class CredentialUploadTooLargeError(QanerisError):
    """An uploaded credential or certificate exceeded the product boundary's size limit.

    The message states the limit and nothing else: never the filename and never a byte of the body.
    """

    code = "credential_upload_too_large"
    status_code = 413


class CredentialKindMismatchError(QanerisError):
    """A credential command was aimed at a secret of the wrong kind.

    ``certificate inspect`` / ``certificate delete`` address certificate and private key material
    only. A password, token or api key that happens to carry a valid ``sec_`` identifier is refused
    here rather than printed or deleted, so the certificate commands cannot be used as a second,
    untyped credential surface.
    """

    code = "credential_kind_mismatch"


class CertificateValidationError(QanerisError):
    """Certificate or private key material could not be accepted.

    Raised when the input is unparseable, outside its validity window, or unusable for the purpose
    it was submitted for. The message never carries the certificate body or the key material.
    """

    code = "certificate_validation_failed"


class CertificateKeyMismatchError(CertificateValidationError):
    """The client certificate and the private key do not belong to the same key pair."""

    code = "certificate_key_mismatch"


class TLSMaterializationError(QanerisError):
    """TLS material could not be turned into driver parameters for a connection attempt.

    Raised on the runtime boundary: the resolved certificate material was unreadable, expired or
    unusable by the time the driver was about to open a socket. A stored certificate can expire and
    an environment or file reference can be repointed between scans, so validation is repeated here
    and fails closed rather than trusting an earlier acceptance.

    The message names the problem and never the material - no certificate body, private key, key
    password, CA content or temporary path content is ever part of it.
    """

    code = "tls_materialization_failed"


class TLSFeatureUnsupportedError(QanerisError):
    """The driver cannot honour a TLS capability the connection profile asked for.

    Raised for a capability the ``TLS_DRIVER_MATRIX`` explicitly records as unsupported for that
    driver - custom CA on SQL Server, mTLS on Qdrant, a TLS 1.3 minimum on a driver that cannot
    enforce it, a hostname override on a driver that cannot carry one. It fails closed: an
    unsupported request is refused, never silently ignored, and never downgraded on the caller's
    behalf (verification is not quietly disabled to make the connection work).
    """

    code = "tls_feature_unsupported"
