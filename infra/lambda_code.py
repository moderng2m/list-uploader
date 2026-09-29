"""Lambda code asset for `backend/`, bundled locally (no Docker required).

Dependencies from backend/requirements.txt are installed for the Lambda
platform (Python 3.12, arm64) with uv if available, else pip.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import jsii
from aws_cdk import BundlingOptions, ILocalBundling
from aws_cdk import aws_lambda as lambda_

BACKEND_DIR = Path(__file__).resolve().parent.parent / "backend"
RUNTIME = lambda_.Runtime.PYTHON_3_12
ARCH = lambda_.Architecture.ARM_64
PACKAGES = ("shared", "bff", "audit_archiver", "tasks")
_DOCKER_BUNDLE_CMD = (
    "pip install -r requirements.txt -t /asset-output && "
    "cp -r shared bff audit_archiver tasks /asset-output/"
)


@jsii.implements(ILocalBundling)
class _LocalBundler:
    def try_bundle(self, output_dir: str, *args: object, **kwargs: object) -> bool:
        out = Path(output_dir)
        reqs = BACKEND_DIR / "requirements.txt"
        if shutil.which("uv"):
            cmd = [
                "uv", "pip", "install", "--quiet", "--target", str(out), "-r", str(reqs),
                "--python-platform", "aarch64-manylinux2014", "--python-version", "3.12",
                "--only-binary", ":all:",
            ]  # fmt: skip
        else:
            cmd = [
                sys.executable, "-m", "pip", "install", "--quiet", "--target", str(out),
                "-r", str(reqs), "--platform", "manylinux2014_aarch64",
                "--python-version", "3.12", "--only-binary=:all:", "--implementation", "cp",
            ]  # fmt: skip
        subprocess.run(cmd, check=True)
        for pkg in PACKAGES:
            shutil.copytree(
                BACKEND_DIR / pkg,
                out / pkg,
                dirs_exist_ok=True,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "tests"),
            )
        return True


def backend_code() -> lambda_.Code:
    return lambda_.Code.from_asset(
        str(BACKEND_DIR),
        exclude=["tests", "**/__pycache__", "*.pyc"],
        bundling=BundlingOptions(
            image=RUNTIME.bundling_image,
            local=_LocalBundler(),
            command=["bash", "-c", _DOCKER_BUNDLE_CMD],
        ),
    )
