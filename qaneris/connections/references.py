"""Structured extraction of managed secret references from a connection profile.

Both the catalog (counting references before a delete) and the application service (deciding which
rotated secrets became obsolete) need to answer "which managed secrets does this profile name?".
The answer must be identical in both places, so it lives here once.

The walk reads the parsed models - ``AuthenticationConfig``, ``TLSConfig`` and their fields - rather
than searching the serialized document. That matters because the question is about meaning, not
text: a re-serialized profile, a differently ordered field or an unrelated string that happens to
contain a ``sec_`` id must never be mistaken for a reference, and a reference must never be missed
because of formatting. It also means a future nesting change cannot silently make a count wrong.

Only opaque identifiers come back out. Nothing here resolves, decrypts or inspects a secret.
"""

from __future__ import annotations

from qaneris.contracts.connection import (
    ConnectionProfile,
    SecretProviderKind,
    SecretReference,
)

#: The fields on the authentication and TLS models that can carry a secret reference. Enumerated
#: explicitly so a newly added reference-bearing field must be considered here on purpose, instead
#: of being picked up - or missed - by a recursive walk over whatever happens to be attached.
_AUTHENTICATION_REFERENCE_FIELDS = (
    "password",
    "token",
    "api_key",
    "private_key",
    "service_account",
)

_TLS_REFERENCE_FIELDS = (
    "ca_certificate",
    "client_certificate",
    "client_private_key",
    "client_private_key_password",
)


def _reference_fields(profile: ConnectionProfile) -> tuple[SecretReference | None, ...]:
    """Every secret-reference-bearing field of a profile, in a fixed order."""
    return (
        *(getattr(profile.authentication, name) for name in _AUTHENTICATION_REFERENCE_FIELDS),
        *(getattr(profile.tls, name) for name in _TLS_REFERENCE_FIELDS),
    )


def managed_secret_references(profile: ConnectionProfile) -> list[SecretReference]:
    """Return every managed reference a profile names, once per occurrence.

    Only ``provider == managed`` references are returned; an ``environment`` identifier is a
    variable name and a ``file`` identifier is a path, and neither names an object in the managed
    credential store. One reference is yielded per field it fills, so a profile that uses the same
    secret as three pieces of TLS material reports three references - which is what a
    reference-count guard needs to be able to refuse a delete.
    """
    return [
        reference
        for reference in _reference_fields(profile)
        if isinstance(reference, SecretReference)
        and reference.provider is SecretProviderKind.MANAGED
    ]


def managed_secret_ids(profile: ConnectionProfile) -> set[str]:
    """Return the set of managed secret ids a connection profile references.

    The set view answers "which secrets", not "how many times": a profile that uses one secret as
    both a password and a CA certificate must not cause a rotation diff to report a phantom
    obsolete secret.
    """
    return {reference.identifier for reference in managed_secret_references(profile)}
