"""Verify published LMS bytes without starting the image or contacting a site."""
import argparse
from html.parser import HTMLParser
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



class AssetReferences(HTMLParser):
    def __init__(self):
        super().__init__()
        self.paths = set()

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "script" and attrs.get("src"):
            self.paths.add(attrs["src"])
        if tag == "link" and attrs.get("rel") in ("stylesheet", "modulepreload"):
            self.paths.add(attrs.get("href", ""))


def verify_frontend(container, report):
    html_path = f"{BENCH}/apps/lms/lms/www/_lms.html"
    member, html = copy_member(container, html_path)
    if not member.isfile():
        raise ValueError("Generated LMS HTML is not a file")
    parser = AssetReferences()
    parser.feed(html.decode())
    assets = []
    css = []
    for url in sorted(parser.paths):
        if not url.startswith("/assets/lms/frontend/"):
            continue
        relative = url.removeprefix("/assets/lms/")
        if posixpath.normpath(relative) != relative or "?" in relative or "#" in relative:
            raise ValueError(f"Unsafe frontend asset reference: {url}")
        if not relative.endswith((".js", ".css")):
            continue
        location = f"{BENCH}/apps/lms/lms/public/{relative}"
        asset, actual = copy_member(container, location)
        if not asset.isfile() or not actual:
            raise ValueError(f"Missing generated frontend bytes: {url}")
        assets.append({"servedPath": url, "imagePath": location,
                       "sha256": digest(actual), "bytes": len(actual)})
        if relative.endswith(".css"):
            css.append(actual.decode())
    if not any(a["servedPath"].endswith(".js") for a in assets) or not css:
        raise ValueError("Generated HTML must reference packaged frontend JS and CSS")
    combined = "\n".join(css)
    assertions = {
        "waveKeyframes": r"@keyframes\s+lms-ai-wave\s*\{",
        "waveAnimation24s": r"animation\s*:\s*lms-ai-wave\s+24s\s+linear\s+infinite",
        "dividerSelector": r"\.lms-ai-divider",
        "leftOnly12px": r"width\s*:\s*12px",
        "travellingTransform": r"translateY\(-50%\)",
        "reducedMotion": r"prefers-reduced-motion\s*:\s*reduce",
    }
    for name, expression in assertions.items():
        if not re.search(expression, combined):
            raise ValueError(f"Compiled frontend CSS lacks {name}")
    report["frontend"] = {"htmlPath": html_path, "htmlSha256": digest(html),
                          "assets": assets, "waveAssertions": list(assertions)}


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
        paths = ["lms/hooks.py", "lms/public/css/mytutor-login.css", "lms/public/js/mytutor-login.js",
                 "lms/public/css/mytutor-common.css", "frontend/src/styles/mytutor.css"]
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
        if (args.source / "lms/public/css/mytutor-common.css").is_file():
            verify_frontend(container, report)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report, indent=2))
    finally:
        subprocess.run(["docker", "rm", "-v", container], check=True)


if __name__ == "__main__":
    main()
