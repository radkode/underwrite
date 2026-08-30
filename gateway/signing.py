"""Asymmetric DSSE signing through a host-owned OpenSSL process."""

import hashlib
import os
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path


class SigningError(RuntimeError):
    """The configured signer is unavailable or rejected the operation."""


_P256_SPKI_PREFIX = bytes.fromhex(
    "3059301306072a8648ce3d020106082a8648ce3d03010703420004"
)
_OPENSSL_COMMAND_SECONDS = 30


def _key_file(path, label):
    path = Path(path)
    if not path.is_absolute():
        raise SigningError(f"{label} path must be absolute")
    if not hasattr(os, "O_NOFOLLOW"):
        raise SigningError("signer requires no-follow file opens")
    try:
        descriptor = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as error:
        raise SigningError(f"{label} cannot be opened") from error
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise SigningError(f"{label} must be a regular file")
        if not 0 < before.st_size <= 1_000_000:
            raise SigningError(f"{label} has an invalid byte size")
        chunks = []
        while True:
            block = os.read(descriptor, 64 * 1024)
            if not block:
                break
            chunks.append(block)
            if sum(map(len, chunks)) > 1_000_000:
                raise SigningError(f"{label} exceeds its byte limit")
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    identity = lambda value: (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )
    data = b"".join(chunks)
    if identity(before) != identity(after) or len(data) != before.st_size:
        raise SigningError(f"{label} changed while it was read")
    if not data:
        raise SigningError(f"{label} must be non-empty")
    return path, before, data


class OpenSSLSigner:
    """Sign PAE bytes without exposing a generic signing endpoint to the job."""

    algorithm = "ecdsa-p256-sha256"

    def __init__(
        self,
        private_key,
        public_key,
        signer_id,
        openssl="openssl",
    ):
        executable = shutil.which(openssl)
        if executable is None:
            raise SigningError("openssl is not available")
        self.openssl = str(Path(executable).resolve())
        self.private_key, private_details, self._private_key_bytes = _key_file(
            private_key, "private key"
        )
        self.public_key, _public_details, self._public_key_bytes = _key_file(
            public_key, "public key"
        )
        if stat.S_IMODE(private_details.st_mode) & 0o077:
            raise SigningError("private key must not be accessible to group or other")
        if not isinstance(signer_id, str) or not signer_id or "\x00" in signer_id:
            raise SigningError("signer identity must be non-empty text")
        self.signer_id = signer_id
        with self._temporary(self._public_key_bytes, "public-key-") as public:
            public_der = self._run(
                [
                    self.openssl,
                    "pkey",
                    "-pubin",
                    "-in",
                    public.name,
                    "-outform",
                    "DER",
                ]
            )
        if len(public_der) != 91 or not public_der.startswith(_P256_SPKI_PREFIX):
            raise SigningError("signing key must be named-curve ECDSA P-256")
        self.key_id = "sha256:" + hashlib.sha256(public_der).hexdigest()
        probe = b"underwrite signer preflight"
        signature = self.sign(probe, self.key_id)
        if self.verify(probe, self.key_id, signature) != self.signer_id:
            raise SigningError("private and public signing keys do not match")

    def _run(self, command, data=None):
        environment = {
            "LANG": "C",
            "LC_ALL": "C",
            "PATH": os.path.dirname(self.openssl),
        }
        try:
            completed = subprocess.run(
                command,
                input=data,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=environment,
                timeout=_OPENSSL_COMMAND_SECONDS,
            )
        except subprocess.TimeoutExpired as error:
            raise SigningError("openssl operation timed out") from error
        except OSError as error:
            raise SigningError("openssl could not be started") from error
        if completed.returncode:
            detail = completed.stderr.decode("utf-8", "replace").strip()
            raise SigningError(f"openssl rejected the signing operation: {detail}")
        return completed.stdout

    def _temporary(self, data, prefix):
        handle = tempfile.NamedTemporaryFile(prefix="underwrite-" + prefix)
        os.chmod(handle.name, 0o600)
        handle.write(data)
        handle.flush()
        return handle

    def sign(self, pae, key_id):
        if not isinstance(pae, bytes) or not pae:
            raise SigningError("signature input must be non-empty bytes")
        if key_id != self.key_id:
            raise SigningError("signature key ID does not match the configured key")
        with self._temporary(self._private_key_bytes, "private-key-") as private:
            signature = self._run(
                [
                    self.openssl,
                    "dgst",
                    "-sha256",
                    "-sign",
                    private.name,
                ],
                pae,
            )
        if not signature:
            raise SigningError("openssl returned an empty signature")
        return signature

    def verify(self, pae, key_id, signature):
        if not isinstance(pae, bytes) or not pae:
            raise SigningError("signature input must be non-empty bytes")
        if key_id != self.key_id:
            raise SigningError("signature key ID does not match the configured key")
        if not isinstance(signature, bytes) or not signature:
            raise SigningError("signature must be non-empty bytes")
        with self._temporary(signature, "signature-") as signature_file, self._temporary(
            self._public_key_bytes, "public-key-"
        ) as public:
            try:
                completed = subprocess.run(
                    [
                        self.openssl,
                        "dgst",
                        "-sha256",
                        "-verify",
                        public.name,
                        "-signature",
                        signature_file.name,
                    ],
                    input=pae,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env={
                        "LANG": "C",
                        "LC_ALL": "C",
                        "PATH": os.path.dirname(self.openssl),
                    },
                    timeout=_OPENSSL_COMMAND_SECONDS,
                )
            except subprocess.TimeoutExpired as error:
                raise SigningError("openssl operation timed out") from error
            except OSError as error:
                raise SigningError("openssl could not be started") from error
        if completed.returncode:
            raise SigningError("signature verification failed")
        return self.signer_id
