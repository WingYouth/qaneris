"""HTTP boundary for credential and certificate upload (RS-CRED-01B).

A product caller can hand over a password as JSON, a CA certificate as a file, or an mTLS client
identity as a certificate/key pair. Those are three genuinely different submissions - a text value,
one opaque blob, and two blobs that only mean something together - so they get three entry points
rather than one endpoint that guesses what the body was.

This module owns four things and no more:

1. **Request validation.** Which kinds a JSON body may carry, which form fields a multipart body
   may carry, and the size of a value. Every refusal happens before the Application Service is
   entered, and every message names the problem and never the material.
2. **Bounded reading.** A multipart body is refused before it is parsed when its declared size is
   already too large, and each part is then streamed in fixed chunks and refused the moment it
   exceeds the limit - so an oversized body is never buffered whole and never spooled to disk.
   Certificate material is held in memory and never staged: unlike an Excel workbook it is small,
   and a private key's filesystem lifetime is worth keeping at zero.
3. **One service call.** The route calls ``QanerisService`` and nothing else. It never touches the
   credential store, the certificate validator, the catalog or the secret resolver.
4. **Product projection.** A response carries ``secret_id`` and safe metadata only.
   ``public_credential()`` makes that projection; the routes do not build response dictionaries by
   hand.

Nothing about secret material may be echoed: not the value, not the certificate body, not the
private key, not the decrypt password, and not the multipart filename or any server path.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool

# ``starlette.datastructures.UploadFile``, not ``fastapi.UploadFile``: the multipart parser
# populates a Starlette form, so its parts are instances of the base class even though FastAPI's
# declared field type is the subclass.
from starlette.datastructures import UploadFile

from qaneris.application.service import QanerisService
from qaneris.common.errors import (
    CredentialUploadTooLargeError,
    DatasourceUnavailableError,
)
from qaneris.contracts.credentials import (
    CREDENTIAL_FILE_MAX_BYTES,
    ManagedClientIdentityPublic,
    ManagedCredentialPublic,
    ManagedSecretKind,
    ManagedTextSecretCreate,
    public_credential,
)

#: Multipart read granularity. The bound is enforced while reading, so the body is never
#: materialized in memory as a whole before it is judged.
CREDENTIAL_STREAM_CHUNK_BYTES = 64 * 1024

#: Longest accepted private key decrypt password, matching the JSON text-secret limit.
PRIVATE_KEY_PASSWORD_MAX_CHARS = 65_536

#: Bytes of multipart framing (boundaries, headers, other fields) allowed on top of the payload
#: bound when judging the declared request size up front.
_MULTIPART_FRAMING_ALLOWANCE = 8 * 1024

#: The one multipart field a CA certificate upload may carry.
CA_FILE_FIELD = "file"

#: The two multipart files an mTLS identity upload carries, plus the optional decrypt password.
CLIENT_CERTIFICATE_FIELD = "certificate"
CLIENT_PRIVATE_KEY_FIELD = "private_key"
PRIVATE_KEY_PASSWORD_FIELD = "private_key_password"


def service_of(request: Request) -> QanerisService:
    """Resolve the single Application Service the process was composed with.

    The credential boundary never builds a second ``QanerisService``, catalog or credential
    store: it uses the one composition installed by ``create_app``.
    """
    service = getattr(request.app.state, "service", None)
    if service is None:  # pragma: no cover - a miscomposed app, not a user-reachable state
        raise DatasourceUnavailableError("应用服务未初始化")
    return service


def missing_field(field: str) -> RequestValidationError:
    """A missing multipart part, reported as FastAPI reports a missing body field."""
    return RequestValidationError(
        [{"type": "missing", "loc": ("body", field), "msg": "Field required", "input": None}]
    )


def refuse_declared_oversize(request: Request, *, limit: int) -> None:
    """Refuse a body that already declares itself too large, before the form is parsed.

    Starlette spools a large part to a temporary file while parsing it, so the size is judged from
    the declared ``Content-Length`` first: an oversized upload is rejected without the multipart
    parser ever seeing it, and therefore without a temporary file ever being created.
    """
    declared = request.headers.get("content-length")
    if declared is None:
        return
    try:
        size = int(declared)
    except ValueError:
        return
    if size > limit + _MULTIPART_FRAMING_ALLOWANCE:
        raise CredentialUploadTooLargeError(
            f"上传文件超过凭据大小限制（最大 {limit} 字节）"
        )


async def read_bounded(upload: UploadFile, *, limit: int = CREDENTIAL_FILE_MAX_BYTES) -> bytes:
    """Read one multipart part into a bounded buffer, refusing it the moment it is too large.

    The part is read in ``CREDENTIAL_STREAM_CHUNK_BYTES`` chunks and the running total is checked
    before each chunk is kept, so an oversized body is refused without being buffered in full and
    without ever being written to disk. An empty part is a request error here rather than a
    validator error later: there is nothing to validate.
    """
    buffer = bytearray()
    while True:
        chunk = await upload.read(CREDENTIAL_STREAM_CHUNK_BYTES)
        if not chunk:
            break
        if len(buffer) + len(chunk) > limit:
            raise CredentialUploadTooLargeError(
                f"上传文件超过凭据大小限制（最大 {limit} 字节）"
            )
        buffer.extend(chunk)
    if not buffer:
        raise ValueError("上传文件为空")
    return bytes(buffer)


async def file_part(
    request: Request, field: str, *, limit: int = CREDENTIAL_FILE_MAX_BYTES
) -> bytes:
    """Extract one required file part from a multipart request as bounded, in-memory bytes.

    A missing part is reported the same way FastAPI reports a missing declared field, so the caller
    sees one stable ``validation_error`` shape. Extra form fields the client may send -
    ``skip_validation``, ``store_raw``, ``no_normalize``, ``allow_expired`` and anything else - have
    no effect whatsoever: no control logic reads them, so there is no validation bypass to find.
    """
    refuse_declared_oversize(request, limit=limit)
    form = await request.form()
    upload = form.get(field)
    if not isinstance(upload, UploadFile):
        raise missing_field(field)
    return await read_bounded(upload, limit=limit)


def optional_password(raw: Any) -> str | None:
    """Normalize the optional private key decrypt password.

    An empty string means "no password", never a password that happens to be empty. The value is
    transient: it decrypts the uploaded key and is not stored, logged or echoed.
    """
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise missing_field(PRIVATE_KEY_PASSWORD_FIELD)
    if len(raw) > PRIVATE_KEY_PASSWORD_MAX_CHARS:
        raise ValueError(
            f"private_key_password 最长为 {PRIVATE_KEY_PASSWORD_MAX_CHARS} 个字符"
        )
    return raw or None


def credential_payload(credential: ManagedCredentialPublic) -> dict[str, object]:
    """The product-safe projection of one managed secret."""
    return credential.model_dump(mode="json")


def identity_payload(identity: ManagedClientIdentityPublic) -> dict[str, object]:
    """The product-safe projection of one stored mTLS client identity."""
    return identity.model_dump(mode="json")


async def call_service(function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Run a blocking Application Service call off the event loop.

    Certificate parsing, encryption and the fsync behind a store write are all synchronous, so the
    route hands the call to a worker thread rather than stalling every other request behind it -
    the same pattern the Excel boundary uses.
    """
    return await run_in_threadpool(function, *args, **kwargs)


# --------------------------------------------------------------------------------------------
# routes
# --------------------------------------------------------------------------------------------


def register_credential_routes(app: FastAPI) -> None:
    """Attach the credential and certificate entry points to the composition."""

    @app.post("/api/credentials", status_code=201)
    def create_credential(request: Request, body: ManagedTextSecretCreate) -> JSONResponse:
        """Store one text secret (password / token / api key / private key password).

        The JSON body carries the four text kinds only; a certificate or private key posted here is
        refused by the request contract, so the file entry points stay the only way certificate
        material can enter.
        """
        info = service_of(request).create_managed_secret(body.kind, body.value)
        return JSONResponse(status_code=201, content=credential_payload(public_credential(info)))

    @app.post("/api/certificates/ca", status_code=201)
    async def upload_ca_certificate(request: Request) -> JSONResponse:
        """Store one CA certificate from a multipart upload.

        The file extension is not inspected: the certificate validator decides whether the bytes are
        a usable CA certificate, and it accepts PEM and DER alike.
        """
        body = await file_part(request, CA_FILE_FIELD)
        service = service_of(request)
        info = await call_service(
            service.create_managed_secret, ManagedSecretKind.CA_CERTIFICATE, body
        )
        return JSONResponse(status_code=201, content=credential_payload(public_credential(info)))

    @app.post("/api/certificates/client-identity", status_code=201)
    async def upload_client_identity(request: Request) -> JSONResponse:
        """Store an mTLS client certificate and its private key as a validated pair.

        The password is used only to decrypt the uploaded key; the stored key is unencrypted PKCS#8,
        so no password secret is created for it. The pair is validated before either half is
        written, and the two writes are rolled back together on failure.
        """
        refuse_declared_oversize(request, limit=2 * CREDENTIAL_FILE_MAX_BYTES)
        form = await request.form()
        certificate = form.get(CLIENT_CERTIFICATE_FIELD)
        if not isinstance(certificate, UploadFile):
            raise missing_field(CLIENT_CERTIFICATE_FIELD)
        private_key = form.get(CLIENT_PRIVATE_KEY_FIELD)
        if not isinstance(private_key, UploadFile):
            raise missing_field(CLIENT_PRIVATE_KEY_FIELD)
        certificate_bytes = await read_bounded(certificate)
        private_key_bytes = await read_bounded(private_key)
        password = optional_password(form.get(PRIVATE_KEY_PASSWORD_FIELD))
        service = service_of(request)
        stored_certificate, stored_key = await call_service(
            service.create_managed_client_identity,
            certificate_bytes,
            private_key_bytes,
            private_key_password=password,
        )
        identity = ManagedClientIdentityPublic(
            client_certificate=public_credential(stored_certificate),
            client_private_key=public_credential(stored_key),
        )
        return JSONResponse(status_code=201, content=identity_payload(identity))

    @app.get("/api/credentials/{secret_id}")
    def inspect_credential(secret_id: str, request: Request) -> dict[str, object]:
        """Return the public record of one managed secret, of any kind."""
        info = service_of(request).inspect_managed_secret(secret_id)
        return credential_payload(public_credential(info))

    @app.delete("/api/credentials/{secret_id}", status_code=204)
    def delete_credential(secret_id: str, request: Request) -> Response:
        """Delete one managed secret, of any kind, while nothing references it."""
        service_of(request).delete_managed_secret(secret_id)
        return Response(status_code=204)
