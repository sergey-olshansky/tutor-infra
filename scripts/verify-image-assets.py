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
        "singleWaveHueKeyframes": r"@keyframes\s+lms-ai-hue\s*\{",
        "singleWaveHueAnimation": r"lms-ai-hue\s+24s\s+ease-in-out\s+infinite",
        "tealWavePeak": r"#49aeb8",
        "blueWavePeak": r"#739fcb",
        "lavenderWavePeak": r"#a399cb",
        "fullCompositeOpacity": r"\.lms-ai-divider\s*\{[^}]*opacity\s*:\s*1[;}]",
        "visible2pxCore": r"(?:#000000d1|rgba\(0,\s*0,\s*0,\s*0?\.82\)|rgb\(0 0 0\s*/\s*0?\.82\))\s+calc\(100%\s*-\s*2px\)",
        "nonlinearInwardFalloff": r"(?:#0000000a|rgba\(0,\s*0,\s*0,\s*0?\.04\)|rgb\(0 0 0\s*/\s*0?\.04\))\s+25%.*?(?:#00000029|rgba\(0,\s*0,\s*0,\s*0?\.16\)|rgb\(0 0 0\s*/\s*0?\.16\))\s+50%.*?(?:#00000052|rgba\(0,\s*0,\s*0,\s*0?\.32\)|rgb\(0 0 0\s*/\s*0?\.32\))\s+75%",
        "coreFalloffBoundary": r"(?:#0006|rgba\(0,\s*0,\s*0,\s*0?\.4\)|rgb\(0 0 0\s*/\s*0?\.4\))\s+calc\(100%\s*-\s*2px\)",
        "repeatingIntensityMask": r"mask-size\s*:\s*100%\s+50%",
        "secondaryBackgroundToken": r"--lms-secondary\s*:\s*#d8eff1",
        "secondaryTextToken": r"--lms-secondary-text\s*:\s*#077581",
        "secondaryPalette": r"\.lms-quiz-secondary\s*\{[^}]*background\s*:\s*var\(--lms-secondary\)[^}]*color\s*:\s*var\(--lms-secondary-text\)",
        "secondaryHover": r"\.lms-quiz-secondary:hover:not\(:disabled\)",
        "secondaryDisabled": r"\.lms-quiz-secondary:disabled\s*\{[^}]*background\s*:\s*var\(--lms-secondary\)",
        "secondaryFocus": r"lms-quiz-secondary[^}]*:focus-visible",
        "leftOnly14px": r"width\s*:\s*14px",
        "travellingTransform": r"translateY\(-50%\)",
        "reducedMotion": r"prefers-reduced-motion\s*:\s*reduce",
    }
    for name, expression in assertions.items():
        if not re.search(expression, combined):
            raise ValueError(f"Compiled frontend CSS lacks {name}")
    if re.search(r"lms-ai-wave-blue|\.lms-ai-divider:{1,2}after", combined):
        raise ValueError("Compiled R7 CSS contains a second painted wave layer")
    # Inspect all emitted lazy JS chunks too; Quiz is not necessarily an entry module.
    result = subprocess.run(["docker", "cp", f"{container}:{BENCH}/apps/lms/lms/public/frontend", "-"], check=True, capture_output=True)
    js_assets = []
    with tarfile.open(fileobj=io.BytesIO(result.stdout)) as archive:
        for member in archive.getmembers():
            if member.isfile() and member.name.endswith(".js"):
                data = archive.extractfile(member).read()
                if b"lms-quiz-secondary" in data and b"Check" in data and b"lms-quiz-primary" in data:
                    js_assets.append({"path": member.name, "sha256": digest(data), "bytes": len(data),
                                      "assertions": ["secondaryQuizAction", "checkLabel", "primaryFinishAction"]})
    if not js_assets:
        raise ValueError("Packaged quiz JS lacks secondary Check and primary Finish signatures")
    report["quizJavascript"] = js_assets
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
    raw = subprocess.check_output(["docker", "buildx", "imagetools", "inspect", "--raw", image])
    if digest(raw) != args.digest.removeprefix("sha256:"):
        raise ValueError("Registry manifest bytes differ from requested digest")
    manifest = json.loads(raw)
    platform_image = image
    mapping = {"requestedDigest": args.digest, "mediaType": manifest.get("mediaType"),
               "rawManifestSha256": "sha256:" + digest(raw)}
    if "manifests" in manifest:
        candidates = [m for m in manifest["manifests"] if m.get("platform", {}).get("os") == "linux"
                      and m.get("platform", {}).get("architecture") == "amd64"]
        if len(candidates) != 1:
            raise ValueError("Expected exactly one linux/amd64 platform manifest")
        platform_digest = candidates[0]["digest"]
        platform_image = f"{REPOSITORY}@{platform_digest}"
        platform_raw = subprocess.check_output(["docker", "buildx", "imagetools", "inspect", "--raw", platform_image])
        if "sha256:" + digest(platform_raw) != platform_digest:
            raise ValueError("Platform manifest bytes differ from index descriptor")
        platform_manifest = json.loads(platform_raw)
        mapping["indexDigest"] = args.digest
        mapping["descriptors"] = manifest["manifests"]
    else:
        platform_digest = args.digest
        platform_manifest = manifest
        mapping["indexDigest"] = None
    mapping["platform"] = {"os": "linux", "architecture": "amd64", "digest": platform_digest,
                           "configDigest": platform_manifest["config"]["digest"]}
    image_config = json.loads(subprocess.check_output(["docker", "image", "inspect", image]))[0]
    if image_config["Id"] != mapping["platform"]["configDigest"] or image_config["Os"] != "linux" or image_config["Architecture"] != "amd64":
        raise ValueError("Pulled image config/platform differs from registry manifest")
    container = subprocess.check_output(["docker", "create", "--network", "none", "--entrypoint", "/bin/true", image], text=True).strip()
    report = {"status": "VERIFIED", "image": image, "revision": args.sha, "checks": [], "digestMapping": mapping}
    try:
        paths = ["lms/hooks.py", "lms/public/css/mytutor-login.css", "lms/public/js/mytutor-login.js",
                 "lms/public/css/mytutor-common.css", "frontend/src/styles/mytutor.css", "frontend/src/components/Quiz.vue"]
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
