#!/usr/bin/env python3
"""Sign a set of release artifacts so common/core.py's self-update mechanism
can verify their authenticity before extracting/executing anything downloaded
from a GitHub release.

Usage:
    python3 scripts/sign_release.py <private_key_b64> <output_dir> <file1> [file2 ...]

Writes <output_dir>/checksums.txt (one "sha256  filename" line per input file,
sorted by filename for a deterministic signature) and
<output_dir>/checksums.txt.sig (the Ed25519 signature of checksums.txt's exact
bytes, base64-encoded). Both files must be uploaded as release assets
alongside the platform installers/archives - do NOT rename them, core.py looks
for these exact filenames in the release's asset list.

The private key must never be committed to the repository. In CI it is read
from a GitHub Actions secret (see .github/workflows/build.yml's sign-release
job) and passed to this script as an environment-expanded argument.
"""
import base64
import hashlib
import sys

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def main():
    if len(sys.argv) < 4:
        print(__doc__)
        sys.exit(1)

    priv_b64, output_dir, *files = sys.argv[1:]

    priv_key = Ed25519PrivateKey.from_private_bytes(base64.b64decode(priv_b64))

    lines = []
    for path in sorted(files):
        with open(path, "rb") as f:
            digest = hashlib.sha256(f.read()).hexdigest()
        # Only the basename goes in checksums.txt - core.py matches release
        # asset filenames, not local build paths.
        import os
        lines.append(f"{digest}  {os.path.basename(path)}")

    checksums = "\n".join(lines) + "\n"
    checksums_bytes = checksums.encode("utf-8")

    signature = priv_key.sign(checksums_bytes)
    signature_b64 = base64.b64encode(signature).decode("ascii")

    with open(f"{output_dir}/checksums.txt", "w", encoding="utf-8") as f:
        f.write(checksums)
    with open(f"{output_dir}/checksums.txt.sig", "w", encoding="utf-8") as f:
        f.write(signature_b64)

    print(f"Signed {len(files)} file(s):")
    print(checksums)


if __name__ == "__main__":
    main()
