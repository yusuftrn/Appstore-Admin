#!/usr/bin/env python3
"""Apple API client and read-only app backup library.

Uses Apple's public API and OpenAPI schema. Python 3.10+ and OpenSSL only.
No passwords, browser cookies, source-account writes, or review submissions.
The normal single-account interface lives in appstore_admin.cli.
"""

from __future__ import annotations

import base64
import collections
import datetime as dt
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
import tempfile

from .security import external_url, private_file, run_openssl, validate_profile, validate_signing_key

API = "https://api.appstoreconnect.apple.com"
SCHEMA_URL = "https://developer.apple.com/sample-code/app-store-connect/app-store-connect-openapi-specification.zip"
FORMAT = 1
EDITABLE = {"PREPARE_FOR_SUBMISSION", "DEVELOPER_REJECTED", "REJECTED", "METADATA_REJECTED"}
LIVE = {"READY_FOR_SALE", "READY_FOR_DISTRIBUTION"}
GLOBAL = {"territories", "appCategories"}
PRICE_POINTS = {"appPricePoints", "subscriptionPricePoints", "inAppPurchasePricePoints"}
COMMERCIAL_TYPES = {"appAvailabilities", "territoryAvailabilities", "appPriceSchedules", "appPrices",
                    "subscriptionAvailabilities", "subscriptionPlanAvailabilities", "subscriptionPrices",
                    "subscriptionIntroductoryOffers", "inAppPurchaseAvailabilities",
                    "inAppPurchasePriceSchedules", "inAppPurchasePrices"}
MEDIA = {"appScreenshots", "appPreviews", "subscriptionImages", "subscriptionImagesV2",
         "inAppPurchaseImages", "inAppPurchaseImagesV2", "subscriptionAppStoreReviewScreenshots",
         "inAppPurchaseAppStoreReviewScreenshots", "appEventScreenshots", "appEventVideoClips",
         "appClipHeaderImages", "appClipAdvancedExperienceImages"}

# Directed ownership edges. Catalogs, users, test devices, analytics, signing keys,
# and all-app queries are intentionally excluded. Price references are fetched via
# include, rather than archiving thousands of unused price points per product.
EDGES = {
    "apps": "appInfos appStoreVersions subscriptionGroups inAppPurchasesV2 appPriceSchedule "
            "appAvailabilityV2 endUserLicenseAgreement subscriptionGracePeriod betaAppLocalizations "
            "betaAppReviewDetail betaLicenseAgreement betaGroups appCustomProductPages appEvents "
            "appEncryptionDeclarations appClips appTags accessibilityDeclarations builds "
            "customerReviews appStoreVersionExperimentsV2 gameCenterDetail promotedPurchases "
            "webhooks androidToIosAppMappingDetails backgroundAssets",
    "appInfos": "appInfoLocalizations ageRatingDeclaration primaryCategory primarySubcategoryOne "
                "primarySubcategoryTwo secondaryCategory secondarySubcategoryOne secondarySubcategoryTwo territoryAgeRatings",
    "appStoreVersions": "appStoreVersionLocalizations appStoreReviewDetail build appStoreVersionPhasedRelease "
                        "appStoreVersionExperiments routingAppCoverage gameCenterAppVersion appClipDefaultExperience",
    "appStoreVersionLocalizations": "appScreenshotSets appPreviewSets searchKeywords",
    "appScreenshotSets": "appScreenshots",
    "appPreviewSets": "appPreviews",
    "appStoreReviewDetails": "appStoreReviewAttachments",
    "subscriptionGroups": "subscriptionGroupLocalizations subscriptions versions",
    "subscriptions": "subscriptionLocalizations appStoreReviewScreenshot images subscriptionAvailability "
                     "planAvailabilities prices introductoryOffers promotionalOffers offerCodes winBackOffers versions",
    "subscriptionGroupVersions": "localizations",
    "subscriptionVersions": "localizations image images",
    "inAppPurchaseVersions": "localizations image images",
    "inAppPurchases": "inAppPurchaseLocalizations appStoreReviewScreenshot images inAppPurchaseAvailability "
                      "iapPriceSchedule offerCodes versions content",
    "subscriptionAvailabilities": "availableTerritories",
    "subscriptionPlanAvailabilities": "availableTerritories",
    "inAppPurchaseAvailabilities": "availableTerritories",
    "appAvailabilities": "territoryAvailabilities",
    "appPriceSchedules": "baseTerritory manualPrices automaticPrices",
    "inAppPurchasePriceSchedules": "baseTerritory manualPrices automaticPrices",
    "subscriptionPromotionalOffers": "prices",
    "subscriptionOfferCodes": "prices",
    "inAppPurchaseOfferCodes": "prices",
    "subscriptionWinBackOffers": "prices",
    "endUserLicenseAgreements": "territories",
    "appCustomProductPages": "appCustomProductPageVersions",
    "appCustomProductPageVersions": "appCustomProductPageLocalizations",
    "appCustomProductPageLocalizations": "appScreenshotSets appPreviewSets",
    "appEvents": "localizations",
    "appEventLocalizations": "appEventScreenshots appEventVideoClips",
    "appEncryptionDeclarations": "appEncryptionDeclarationDocument",
    "appClips": "appClipDefaultExperiences appClipAdvancedExperiences",
    "appClipDefaultExperiences": "appClipDefaultExperienceLocalizations appClipAppStoreReviewDetail",
    "appClipDefaultExperienceLocalizations": "appClipHeaderImage",
    "appClipAdvancedExperiences": "headerImage localizations",
    "appStoreVersionExperiments": "appStoreVersionExperimentTreatments",
    "appStoreVersionExperimentsV2": "appStoreVersionExperimentTreatments",
    "appStoreVersionExperimentTreatments": "appStoreVersionExperimentTreatmentLocalizations",
    "appStoreVersionExperimentTreatmentLocalizations": "appScreenshotSets appPreviewSets",
    "gameCenterDetails": "gameCenterLeaderboards gameCenterLeaderboardSets gameCenterAchievements",
    "gameCenterAchievements": "localizations",
    "gameCenterAchievementLocalizations": "gameCenterAchievementImage",
    "gameCenterLeaderboards": "localizations",
    "gameCenterLeaderboardLocalizations": "gameCenterLeaderboardImage",
    "customerReviews": "response",
    "backgroundAssets": "versions",
    "backgroundAssetVersions": "backgroundAssetUploadFiles",
}
MANUAL = [
    "Backups contain private app, review, product and commercial records; keep them outside source control.",
    "Only data and downloadable assets exposed by Apple's API can be archived; inspect missing assets and read errors.",
    "Build binaries, signing, privacy questionnaires, review submission and release are outside this package's scope.",
    "Backups are for inspection and preservation; this package does not restore or migrate app records.",
]


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def key(resource):
    return resource["type"] + ":" + resource["id"]


def linkage(resource):
    return {"type": resource["type"], "id": resource["id"]}


def slug(value):
    return re.sub(r"[^A-Za-z0-9._-]", "_", str(value))[:140] or "asset"


def save_json(path, value, overwrite=True):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".json-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        if overwrite:
            os.replace(temporary, path)
        else:
            # Atomic exclusive publication also refuses existing symlinks.
            os.link(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def digest(path, algorithm="sha256"):
    if algorithm not in {"sha256", "md5"}:
        raise RuntimeError("Unsupported file checksum algorithm")
    h = hashlib.new(algorithm, usedforsecurity=(algorithm != "md5"))
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


class ApiError(RuntimeError):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ApiError(code, "Unexpected API redirect; refused to forward credentials")


class HttpsRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        external_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def der_signature_to_raw(signature):
    """OpenSSL emits DER ECDSA; JWT ES256 needs 32-byte r followed by s."""
    def length(pos):
        first = signature[pos]
        if first < 128:
            return first, pos + 1
        count = first & 127
        if not 1 <= count <= 2:
            raise ValueError("Invalid DER length")
        return int.from_bytes(signature[pos + 1:pos + 1 + count], "big"), pos + 1 + count
    if not signature or signature[0] != 0x30:
        raise ValueError("Invalid ECDSA signature")
    size, pos = length(1)
    if pos + size != len(signature):
        raise ValueError("Invalid ECDSA sequence")
    result = b""
    for _ in range(2):
        if signature[pos] != 0x02:
            raise ValueError("Invalid ECDSA integer")
        size, pos = length(pos + 1)
        part = signature[pos:pos + size]
        if not part or part[0] & 128:
            raise ValueError("Negative ECDSA integer")
        integer = int.from_bytes(part, "big")
        result += integer.to_bytes(32, "big")
        pos += size
    if pos != len(signature):
        raise ValueError("Trailing ECDSA bytes")
    return result


class Client:
    def __init__(self, profile, readonly=True):
        profile_path = private_file(profile)
        self.profile = validate_profile(read_json(profile_path))
        self.private_key = Path(self.profile["private_key"])
        if not self.private_key.is_absolute():
            self.private_key = profile_path.parent / self.private_key
        self.private_key = validate_signing_key(self.private_key)
        self.readonly = readonly
        self.token = None
        self.token_expiry = 0
        self.opener = urllib.request.build_opener(NoRedirect())

    def bearer(self):
        if self.token and time.time() < self.token_expiry - 60:
            return self.token
        timestamp = int(time.time())
        payload = {"iat": timestamp - 10, "exp": timestamp + 900, "aud": "appstoreconnect-v1"}
        if self.profile.get("issuer_id"):
            payload["iss"] = self.profile["issuer_id"]
        else:
            payload["sub"] = "user"
        header = {"alg": "ES256", "kid": self.profile["key_id"], "typ": "JWT"}
        def encode(value):
            data = value if isinstance(value, bytes) else json.dumps(value, separators=(",", ":")).encode()
            return base64.urlsafe_b64encode(data).rstrip(b"=").decode()
        unsigned = encode(header) + "." + encode(payload)
        private_file(self.private_key)
        proc = run_openssl(["dgst", "-sha256", "-sign", str(self.private_key)], unsigned.encode())
        if proc.returncode:
            raise RuntimeError("OpenSSL could not sign with the API .p8 key")
        self.token = unsigned + "." + encode(der_signature_to_raw(proc.stdout))
        self.token_expiry = payload["exp"]
        return self.token

    def request(self, method, path, body=None, params=None):
        if self.readonly and method != "GET":
            raise RuntimeError("Source/read-only client refuses writes")
        url = path if path.startswith("https://") else API + path
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "https" or parsed.netloc != "api.appstoreconnect.apple.com":
            raise RuntimeError("Refused API request outside Apple's API host")
        if not re.fullmatch(r'/v[12]/[A-Za-z0-9_/-]+', parsed.path) or parsed.fragment:
            raise RuntimeError("Refused malformed Apple API resource path")
        if method not in {"GET", "POST", "PATCH"}:
            raise RuntimeError("Unsupported API method; deletion and publication are unavailable")
        writable = "appStoreVersions|appInfoLocalizations|appStoreVersionLocalizations|appScreenshotSets|appScreenshots"
        if method == "POST" and not re.fullmatch(r'/v1/(?:' + writable + ')', parsed.path):
            raise RuntimeError("Unsupported API write endpoint")
        if method == "PATCH" and not re.fullmatch(r'/v1/(?:' + writable + r')/[A-Za-z0-9-]+(?:/relationships/appScreenshots)?', parsed.path):
            raise RuntimeError("Unsupported API write endpoint")
        if params:
            url += ("&" if parsed.query else "?") + urllib.parse.urlencode(params)
        for attempt in range(5):
            headers = {"Authorization": "Bearer " + self.bearer(), "Accept": "application/json"}
            data = None
            if body is not None:
                headers["Content-Type"] = "application/json"
                data = json.dumps(body).encode()
            request = urllib.request.Request(url, data=data, headers=headers, method=method)
            try:
                with self.opener.open(request, timeout=90) as response:
                    raw = response.read()
                return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as error:
                raw = error.read()
                if error.code == 401 and attempt == 0:
                    self.token = None
                    continue
                # Never replay ambiguous POST errors: a create might have committed.
                retry = error.code == 429 or (method == "GET" and error.code in {500, 502, 503, 504})
                if retry and attempt < 4:
                    try:
                        delay = float(error.headers.get("Retry-After", 2 ** attempt))
                    except ValueError:
                        delay = 2 ** attempt
                    time.sleep(min(delay, 30))
                    continue
                try:
                    errors = json.loads(raw).get("errors", [])
                    codes = [e.get("code") for e in errors if isinstance(e, dict)]
                    codes = [c for c in codes if isinstance(c, str) and re.fullmatch(r'[A-Z0-9._-]{1,80}', c)]
                    message = "; ".join(codes[:5]) or "API request rejected"
                except (ValueError, AttributeError, TypeError):
                    message = "API request rejected"
                # Apple details may echo metadata, review credentials or signed URLs.
                raise ApiError(error.code, f"{method} {parsed.path}: HTTP {error.code}: {message}") from None

            except (urllib.error.URLError, TimeoutError):
                if method == "GET" and attempt < 4:
                    time.sleep(2 ** attempt)
                    continue
                raise ApiError(0, f"Network failure during {method} {parsed.path}; rerun to reconcile resources") from None
        raise RuntimeError("API retries exhausted")

    def pages(self, path, params=None):
        result, included, visited = [], [], set()
        while path:
            if path in visited:
                raise RuntimeError("Pagination cycle detected")
            visited.add(path)
            response = self.request("GET", path, params=params)
            value = response.get("data")
            result.extend(value if isinstance(value, list) else [value] if value else [])
            included.extend(response.get("included", []))
            path = response.get("links", {}).get("next")
            params = None
        return result, included


class Schema:
    def __init__(self, document):
        self.document = document
        self.paths = document["paths"]

    def resolve(self, value):
        while "$ref" in value:
            name = value["$ref"].split("/")[-1]
            value = self.document["components"]["schemas"][name]
        return value

    def endpoint(self, kind, method="get", relation=None, version=None):
        # inAppPurchases v1 is read-only/legacy; prefer the v2 API.
        ending = f"/{kind}" + ("/{id}" if method != "post" or relation else "")
        if relation:
            ending += "/" + relation
        candidates = [p for p, operations in self.paths.items() if p.endswith(ending) and method in operations]
        if version is not None:
            candidates = [p for p in candidates if p.startswith(f"/v{version}/")]
        if not candidates:
            return None
        return sorted(candidates, key=lambda p: int(p.split("/")[1][1:]), reverse=True)[0]

    def response_version(self, template):
        response = self.paths[template]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
        data = self.resolve(response).get("properties", {}).get("data", {})
        ref = data.get("items", data).get("$ref", "").split("/")[-1]
        return 2 if ref.endswith("V2") else 1

    def parameters(self, template):
        operation = self.paths[template]["get"]
        params = {}
        for item in operation.get("parameters", []):
            item = self.resolve(item)
            if item["name"] == "limit":
                params["limit"] = min(200, item.get("schema", {}).get("maximum", 200))
            if item["name"] == "include":
                options = item.get("schema", {}).get("items", {}).get("enum", [])
                wanted = [x for x in options if x in {"territory", "appPricePoint", "subscriptionPricePoint", "inAppPurchasePricePoint"}]
                if wanted:
                    params["include"] = ",".join(wanted)
        return params

    def request_schema(self, kind, method, version=None):
        template = self.endpoint(kind, method, version=version)
        if not template:
            return None
        body = self.paths[template][method]["requestBody"]["content"]["application/json"]["schema"]
        return self.resolve(body)["properties"]["data"]

    def payload(self, kind, method, attrs=None, relationships=None, resource_id=None, version=None):
        schema = self.request_schema(kind, method, version)
        if not schema:
            raise RuntimeError(f"No public {method.upper()} endpoint for {kind}")
        props = schema["properties"]
        attr_schema = self.resolve(props.get("attributes", {}))
        rel_schema = self.resolve(props.get("relationships", {}))
        data = {"type": kind}
        if method == "patch":
            data["id"] = resource_id
        values = {k: v for k, v in (attrs or {}).items()
                  if k in attr_schema.get("properties", {}) and v is not None
                  and not attr_schema["properties"][k].get("deprecated")}
        rels = {k: v for k, v in (relationships or {}).items() if k in rel_schema.get("properties", {})}
        for name in attr_schema.get("required", []):
            if name not in values:
                raise RuntimeError(f"Missing required {kind} attribute: {name}")
        for name in rel_schema.get("required", []):
            if name not in rels:
                raise RuntimeError(f"Missing required {kind} relationship: {name}")
        if values or "attributes" in schema.get("required", []):
            data["attributes"] = values
        if rels or "relationships" in schema.get("required", []):
            data["relationships"] = rels
        return {"data": data}


def schema_at(path):
    path = Path(path)
    if not path.is_file():
        raise RuntimeError(f"Schema not found: {path}. Run init-schema first.")
    return Schema(read_json(path))


def init_schema(args):
    destination = Path(args.out)
    if destination.exists():
        raise RuntimeError("Schema output already exists; choose a new filename")
    request = urllib.request.Request(SCHEMA_URL, headers={"User-Agent": "AppStoreAdmin/1"})
    with urllib.request.build_opener(HttpsRedirect()).open(request, timeout=90) as response:
        archive = response.read(32 * 1024 * 1024 + 1)
    if len(archive) > 32 * 1024 * 1024:
        raise RuntimeError("Schema archive exceeds size limit")
    with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
        if bundle.getinfo("openapi.oas.json").file_size > 64 * 1024 * 1024:
            raise RuntimeError("Expanded schema exceeds size limit")
        document = json.loads(bundle.read("openapi.oas.json"))
    save_json(destination, document, overwrite=False)
    print(f"Saved Apple schema {document['info']['version']} to {destination}")


def download(url, destination):
    external_url(url)
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    opener = urllib.request.build_opener(HttpsRedirect())
    for attempt in range(4):
        fd, partial = tempfile.mkstemp(prefix=".asset-", dir=destination.parent)
        try:
            with os.fdopen(fd, "wb") as output:
                request = urllib.request.Request(url, headers={"User-Agent": "AppStoreAdmin/1"})
                with opener.open(request, timeout=90) as response:
                    if "text/html" in response.headers.get("Content-Type", ""):
                        raise RuntimeError("Asset download returned HTML")
                    total = 0
                    for chunk in iter(lambda: response.read(1024 * 1024), b""):
                        total += len(chunk)
                        if total > 1024 * 1024 * 1024:
                            raise RuntimeError("Asset exceeds the 1 GiB download limit")
                        output.write(chunk)
            if not Path(partial).stat().st_size:
                raise RuntimeError("Empty media download")
            with open(partial, "rb") as handle:
                prefix = handle.read(64).lstrip().lower()
            if prefix.startswith((b"#extm3u", b"<!doctype html", b"<html")):
                raise RuntimeError("Asset URL returned a playlist/webpage, not a downloadable media file")
            os.replace(partial, destination)
            return
        except (urllib.error.URLError, TimeoutError):
            if attempt == 3:
                raise RuntimeError("Asset download failed; inspect the backup report") from None
            time.sleep(2 ** attempt)
        finally:
            if os.path.exists(partial):
                os.unlink(partial)


def asset_location(resource):
    attrs = resource.get("attributes", {})
    if attrs.get("videoUrl"):
        return attrs["videoUrl"], ".mp4", "apple-video"
    image = attrs.get("imageAsset") or attrs.get("previewImage")
    if image and image.get("templateUrl"):
        url = image["templateUrl"]
        for name, value in {"w": image.get("width"), "h": image.get("height"), "f": "png"}.items():
            if "{" + name + "}" in url:
                if value is None:
                    raise RuntimeError("Image template has no full-resolution dimensions")
                url = url.replace("{" + name + "}", str(value))
        if "{" in url:
            raise RuntimeError("Unsupported image template placeholder")
        return url, ".png", "full-resolution-rendition"
    for name in ("assetUrl", "downloadUrl", "url"):
        if attrs.get(name) and resource["type"] in MEDIA | {"appEncryptionDeclarationDocuments", "routingAppCoverages"}:
            return attrs[name], Path(attrs.get("fileName", "asset.bin")).suffix or ".bin", "download"
    return None


class Archive:
    def __init__(self, client, schema, directory):
        self.client, self.schema, self.directory = client, schema, Path(directory)
        self.document = {"format": FORMAT, "createdAt": now(), "resources": {}, "edges": {},
                         "assets": {}, "errors": [], "absent": [], "unsupportedEdges": [],
                         "manual": MANUAL, "schemaVersion": schema.document["info"]["version"]}
        self.document["excludedRootEndpoints"] = [
            p for p, operations in schema.paths.items()
            if p.startswith("/v1/apps/{id}/") and p.count("/") == 4 and "get" in operations
            and p.rsplit("/", 1)[-1] not in EDGES["apps"].split()
        ]

    def remember(self, resource):
        identity = key(resource)
        old = self.document["resources"].get(identity, {})
        old.update({k: v for k, v in resource.items() if k != "relationships"})
        old.setdefault("relationships", {}).update(resource.get("relationships", {}))
        self.document["resources"][identity] = old

    def checkpoint(self):
        save_json(self.directory / "manifest.json", self.document)

    def backup(self, app_id):
        if self.directory.exists() and any(self.directory.iterdir()):
            raise RuntimeError("Backup directory must be empty; use a new directory for each snapshot")
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.directory, 0o700)
        save_json(self.directory / "openapi.json", self.schema.document)
        app = self.client.request("GET", f"/v1/apps/{app_id}")["data"]
        self.document["sourceApp"] = linkage(app)
        self.document["sourceBundleId"] = app["attributes"]["bundleId"]
        self.remember(app)
        queue = collections.deque([app])
        visited = set()
        while queue:
            parent = queue.popleft()
            identity = key(parent)
            if identity in visited:
                continue
            visited.add(identity)
            kind = parent["type"]
            for relation in EDGES.get(kind, "").split():
                template = self.schema.endpoint(kind, relation=relation)
                edge_id = identity + "/" + relation
                if not template:
                    self.document["unsupportedEdges"].append(edge_id)
                    continue
                path = template.replace("{id}", parent["id"])
                try:
                    children, included = self.client.pages(path, self.schema.parameters(template))
                    for resource in included:
                        self.remember(resource)
                    for resource in children:
                        resource["_apiVersion"] = self.schema.response_version(template)
                        self.remember(resource)
                    self.document["edges"][edge_id] = [key(child) for child in children]
                    self.document["resources"][identity].setdefault("relationships", {})[relation] = {
                        "data": [linkage(child) for child in children]}
                    queue.extend(child for child in children if child["type"] not in GLOBAL | PRICE_POINTS)
                    print(f"Archive {kind}/{relation}: {len(children)}", flush=True)
                except ApiError as error:
                    if error.status == 404:
                        self.document["edges"][edge_id] = []
                        self.document["absent"].append(edge_id)
                    else:
                        self.document["errors"].append({"edge": edge_id, "error": str(error)})
                        print(f"INCOMPLETE: {edge_id}: {error}", file=sys.stderr)
                self.checkpoint()
        # The icon relationship has no public endpoint in some schema versions.
        self.archive_public_icon(app)
        for identity, resource in list(self.document["resources"].items()):
            if resource["type"] in MEDIA or resource["type"].endswith(("Images", "Screenshots", "Documents", "Coverages", "Attachments")):
                self.archive_asset(resource)
        self.document["finishedAt"] = now()
        self.document["complete"] = not self.document["errors"] and all("path" in entry for entry in self.document["assets"].values())
        self.checkpoint()
        save_json(self.directory / "report.json", self.report())
        print(f"Backup saved: {self.directory.resolve()}; complete={self.document['complete']}")
        return 0 if self.document["complete"] else 2

    def archive_public_icon(self, app):
        bundle = app["attributes"].get("bundleId")
        try:
            url = "https://itunes.apple.com/lookup?" + urllib.parse.urlencode({"bundleId": bundle})
            with urllib.request.build_opener(HttpsRedirect()).open(url, timeout=30) as response:
                results = json.load(response).get("results", [])
            result = next((r for r in results if str(r.get("trackId")) == app["id"]), None)
            if result:
                save_json(self.directory / "public-store-listing.json", result)
                if result.get("artworkUrl512"):
                    download(result["artworkUrl512"], self.directory / "public-icon.png")
        except (OSError, ValueError, RuntimeError) as error:
            self.document["publicListingNote"] = f"Public listing/icon not available: {type(error).__name__}"

    def archive_asset(self, resource):
        identity = key(resource)
        try:
            location = asset_location(resource)
            if not location:
                self.document["assets"][identity] = {"error": "API exposes no downloadable binary URL; supply original file manually"}
                self.checkpoint()
                return
            url, suffix, representation = location
            if not re.fullmatch(r"\.[A-Za-z0-9]{1,10}", suffix):
                raise RuntimeError("Unsafe asset filename extension")
            relative = Path("assets") / slug(resource["type"]) / (slug(resource["id"]) + suffix)
            path = self.directory / relative
            download(url, path)
            self.document["assets"][identity] = {
                "path": relative.as_posix(), "sha256": digest(path), "fileSize": path.stat().st_size,
                "representation": representation,
                "originalFileName": resource.get("attributes", {}).get("fileName"),
            }
            print(f"Downloaded {identity}: {path.stat().st_size} bytes", flush=True)
        except (OSError, RuntimeError, ValueError) as error:
            self.document["assets"][identity] = {"error": f"{type(error).__name__}: {error}"}
            self.document["errors"].append({"asset": identity, "error": str(error)})
        self.checkpoint()

    def report(self):
        counts = collections.Counter(r["type"] for r in self.document["resources"].values())
        return {"resourceCounts": dict(counts), "downloadedAssets": sum("path" in a for a in self.document["assets"].values()),
                "missingAssets": {k: v for k, v in self.document["assets"].items() if "error" in v},
                "readErrors": self.document["errors"], "unsupportedEdges": self.document["unsupportedEdges"],
                "excludedRootEndpoints": self.document["excludedRootEndpoints"],
                "manual": MANUAL}


class Snapshot:
    def __init__(self, directory):
        self.directory = Path(directory).resolve()
        self.document = read_json(self.directory / "manifest.json")
        if self.document.get("format") != FORMAT:
            raise RuntimeError("Unsupported backup format")
        self.resources = self.document["resources"]
        self.schema = schema_at(self.directory / "openapi.json")

    def root(self):
        return self.resources[key(self.document["sourceApp"])]

    def children(self, parent, relation):
        return [self.resources[k] for k in self.document["edges"].get(key(parent) + "/" + relation, [])]

    def asset(self, resource):
        entry = self.document["assets"].get(key(resource), {})
        if "path" not in entry:
            raise RuntimeError(entry.get("error", "Asset not archived"))
        path = (self.directory / entry["path"]).resolve()
        if not path.is_relative_to(self.directory):
            raise RuntimeError("Asset path escapes backup directory")
        if not path.is_file() or digest(path) != entry["sha256"]:
            raise RuntimeError("Asset missing or SHA-256 differs from backup")
        return path

    def verify(self):
        failures = []
        if not self.document.get("finishedAt"):
            failures.append("Backup was interrupted before completion; make a new full snapshot")
        for identity, entry in self.document["assets"].items():
            if "path" in entry:
                try:
                    self.asset(self.resources[identity])
                except RuntimeError as error:
                    failures.append(f"{identity}: {error}")
            else:
                failures.append(f"{identity}: {entry.get('error', 'Missing asset')}")
        failures.extend(e["error"] for e in self.document.get("errors", []))
        return failures



def state_of(resource):
    attrs = resource.get("attributes", {})
    return attrs.get("appVersionState") or attrs.get("appStoreState") or attrs.get("appStoreVersionState") or attrs.get("state")


def version_key(value):
    return tuple(int(x) for x in re.findall(r"\d+", value))


def describe(snapshot):
    doc = snapshot.document
    versions = [{"id": r["id"], **r.get("attributes", {})} for r in snapshot.children(snapshot.root(), "appStoreVersions")]
    infos = [{"id": r["id"], "state": state_of(r)} for r in snapshot.children(snapshot.root(), "appInfos")]
    return {"source": snapshot.root().get("attributes"), "versions": versions, "appInfos": infos,
            "resourceCounts": dict(collections.Counter(r["type"] for r in snapshot.resources.values())),
            "downloadedAssets": sum("path" in a for a in doc["assets"].values()),
            "incomplete": snapshot.verify(), "unsupportedEdges": doc.get("unsupportedEdges", []),
            "excludedRootEndpoints": doc.get("excludedRootEndpoints", []), "manual": MANUAL}

