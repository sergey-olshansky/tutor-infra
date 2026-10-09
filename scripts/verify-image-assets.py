"""Verify published LMS bytes without starting the image or contacting a site."""
import argparse
import hashlib
import io
import json
import pathlib
import posixpath
import re
import subprocess
import tarfile

BENCH = "/home/frappe/frappe-bench"
REPOSITORY = "ghcr.io/sergey-olshansky/tutor-learning"


def digest(data):
    return hashlib.sha256(data).hexdigest()


def copy_member(container, location):
    result = subprocess.run(["docker", "cp", f"{container}:{location}", "-"], check=True, capture_output=True)
    with tarfile.open(fileobj=io.BytesIO(result.stdout)) as archive:
        members = archive.getmembers()
        if len(members) != 1:
            raise ValueError(f"Expected one image member: {location}")
        member = members[0]
        if member.isfile():
            return member, archive.extractfile(member).read()
        return member, None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--digest", required=True)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--source", type=pathlib.Path, required=True)
    parser.add_argument("--output", type=pathlib.Path, required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", args.digest) or not re.fullmatch(r"[a-f0-9]{40}", args.sha):
        parser.error("Expected an immutable image digest and full LMS SHA")
    image = f"{REPOSITORY}@{args.digest}"
    subprocess.run(["docker", "pull", image], check=True)
    container = subprocess.check_output(["docker", "create", "--network", "none", "--entrypoint", "/bin/true", image], text=True).strip()
    report = {"status": "VERIFIED", "image": image, "revision": args.sha, "checks": []}
    try:
        paths = ["lms/hooks.py", "lms/public/css/mytutor-login.css", "lms/public/js/mytutor-login.js"]
        for relative in paths:
            source = args.source / relative
            if not source.is_file():
                if relative == "lms/hooks.py":
                    raise ValueError("Pinned LMS hooks.py is missing")
                continue  # Older tasks need not contain this optional presentation layer.
            member, actual = copy_member(container, f"{BENCH}/apps/lms/{relative}")
            if not member.isfile() or digest(actual) != digest(source.read_bytes()):
                raise ValueError(f"Published image differs from pinned source: {relative}")
            report["checks"].append({"path": relative, "sha256": digest(actual)})
        member, _ = copy_member(container, f"{BENCH}/assets/lms")
        if not member.issym():
            raise ValueError("Expected the packaged Frappe LMS asset symlink")
        target = posixpath.normpath(posixpath.join(f"{BENCH}/sites/assets", member.linkname))
        if target != f"{BENCH}/apps/lms/lms/public":
            raise ValueError(f"Invalid LMS served asset target: {target}")
        report["servedAssetRoot"] = {"path": "/assets/lms", "packagedLink": member.linkname, "runtimeTarget": target}
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report, indent=2))
    finally:
        subprocess.run(["docker", "rm", "-v", container], check=True)


if __name__ == "__main__":
    main()
