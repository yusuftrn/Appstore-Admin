"""Offline archive, authentication and bundled Apple schema regression tests.
"""
import argparse
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from appstore_admin import core as m


def resource(kind, identity, **attributes):
    return {"type": kind, "id": identity, "attributes": attributes}


def small_schema():
    """Minimal JSON:API schema fixture, including versioned commercial metadata."""
    paths, models = {}, {}
    declarations = {
        "apps": (1, ["primaryLocale", "bundleId", "contentRightsDeclaration"], [], [], []),
        "appInfos": (1, [], [], ["primaryCategory"], []),
        "appInfoLocalizations": (1, ["locale", "name", "subtitle", "privacyPolicyUrl"], ["locale", "name"], ["appInfo"], ["appInfo"]),
        "appStoreVersions": (1, ["platform", "versionString", "copyright", "releaseType"], ["platform", "versionString"], ["app"], ["app"]),
        "appStoreVersionLocalizations": (1, ["locale", "description", "keywords", "whatsNew"], ["locale"], ["appStoreVersion"], ["appStoreVersion"]),
        "appScreenshotSets": (1, ["screenshotDisplayType"], ["screenshotDisplayType"], ["appStoreVersionLocalization"], []),
        "appScreenshots": (1, ["fileName", "fileSize", "uploaded", "sourceFileChecksum"], ["fileName", "fileSize"], ["appScreenshotSet"], ["appScreenshotSet"]),
        "ageRatingDeclarations": (1, ["unrestrictedWebAccess", "gambling"], [], [], []),
        "subscriptionGroups": (1, ["referenceName"], ["referenceName"], ["app"], ["app"]),
        "subscriptions": (1, ["productId", "name", "subscriptionPeriod"], ["productId", "name"], ["group"], ["group"]),
        "subscriptionLocalizations": (1, ["locale", "name", "description"], ["locale", "name"], ["subscription"], ["subscription"]),
        "subscriptionLocalizationsV2": (2, ["locale", "name", "description"], ["locale", "name"], ["version"], ["version"]),
        "subscriptionVersions": (1, [], [], ["subscription"], ["subscription"]),
        "subscriptionPrices": (1, ["startDate", "preserveCurrentPrice"], [], ["subscription", "subscriptionPricePoint", "territory"], ["subscription", "subscriptionPricePoint"]),
    }
    refs = {"app": "apps", "appInfo": "appInfos", "appStoreVersion": "appStoreVersions",
            "appStoreVersionLocalization": "appStoreVersionLocalizations", "appScreenshotSet": "appScreenshotSets",
            "primaryCategory": "appCategories", "group": "subscriptionGroups", "subscription": "subscriptions",
            "version": "subscriptionVersions", "subscriptionPricePoint": "subscriptionPricePoints", "territory": "territories"}
    for model, (version, attributes, required_attrs, relationships, required_rels) in declarations.items():
        kind = model.removesuffix("V2")
        models[model] = {"type": "object", "properties": {"type": {"enum": [kind]}}}
        prefix = f"/v{version}/{kind}"
        paths[prefix + "/{id}"] = {"get": {"responses": {"200": {"content": {"application/json": {"schema": {
            "properties": {"data": {"$ref": "#/components/schemas/" + model}}}}}}}}}
        for method, path in (("post", prefix), ("patch", prefix + "/{id}")):
            body = {"properties": {"data": {"properties": {
                "attributes": {"properties": {a: {"type": "string"} for a in attributes}, "required": required_attrs if method == "post" else []},
                "relationships": {"properties": {r: {"properties": {"data": {"type": "object", "properties": {"type": {"enum": [refs[r]]}}}}}
                                                   for r in relationships}, "required": required_rels if method == "post" else []},
            }}}}
            paths.setdefault(path, {})[method] = {"requestBody": {"content": {"application/json": {"schema": body}}}}
    relations = {
        "apps": {"appInfos": "appInfos", "appStoreVersions": "appStoreVersions", "subscriptionGroups": "subscriptionGroups"},
        "appInfos": {"appInfoLocalizations": "appInfoLocalizations", "ageRatingDeclaration": "ageRatingDeclarations", "primaryCategory": "appCategories"},
        "appStoreVersions": {"appStoreVersionLocalizations": "appStoreVersionLocalizations"},
        "appStoreVersionLocalizations": {"appScreenshotSets": "appScreenshotSets"},
        "appScreenshotSets": {"appScreenshots": "appScreenshots"},
        "subscriptionGroups": {"subscriptions": "subscriptions"},
        "subscriptions": {"subscriptionLocalizations": "subscriptionLocalizations", "prices": "subscriptionPrices", "pricePoints": "subscriptionPricePoints", "versions": "subscriptionVersions"},
        "subscriptionVersions": {"localizations": "subscriptionLocalizationsV2"},
    }
    models["appCategories"] = {"properties": {"type": {"enum": ["appCategories"]}}}
    models["subscriptionPricePoints"] = {"properties": {"type": {"enum": ["subscriptionPricePoints"]}}}
    for parent, edges in relations.items():
        for relation, child in edges.items():
            paths[f"/v1/{parent}/{{id}}/{relation}"] = {"get": {"parameters": [{"name": "limit", "schema": {"maximum": 200}}],
                "responses": {"200": {"content": {"application/json": {"schema": {
                    "properties": {"data": {"type": "array", "items": {"$ref": "#/components/schemas/" + child}}}}}}}}}}
    for model in ("apps", "appInfos", "ageRatingDeclarations"):
        paths.pop(f"/v1/{model}", None)  # Server supplies app info and age declarations.
    paths["/v1/appScreenshotSets/{id}/relationships/appScreenshots"] = {"patch": {}}
    return {"info": {"version": "test"}, "paths": paths, "components": {"schemas": models}}


def populate_backup(directory, media=True):
    schema = small_schema()
    app = resource("apps", "source", name="Example App", bundleId="old.bundle", primaryLocale="en-US")
    info = resource("appInfos", "info", appStoreState="READY_FOR_SALE")
    version = resource("appStoreVersions", "version", platform="IOS", versionString="1.0.5", appStoreState="READY_FOR_SALE")
    locale = resource("appInfoLocalizations", "info-locale", locale="en-US", name="Example App", subtitle="Privacy")
    text = resource("appStoreVersionLocalizations", "version-locale", locale="en-US", description="Privacy browser", keywords="privacy,browser", whatsNew="Old release notes")
    category = resource("appCategories", "UTILITIES")
    age = resource("ageRatingDeclarations", "age", unrestrictedWebAccess=True, gambling=False)
    group = resource("subscriptionGroups", "group", referenceName="Premium")
    sub = resource("subscriptions", "sub", productId="premium.monthly", name="Monthly", subscriptionPeriod="ONE_MONTH")
    subloc = resource("subscriptionLocalizations", "sub-locale", locale="en-US", name="Premium", description="Monthly access")
    resources = [app, info, version, locale, text, category, age, group, sub, subloc]
    for entry in resources:
        entry["_apiVersion"] = 1
    edges = {
        "apps:source/appInfos": [m.key(info)], "apps:source/appStoreVersions": [m.key(version)],
        "apps:source/subscriptionGroups": [m.key(group)],
        "appInfos:info/appInfoLocalizations": [m.key(locale)], "appInfos:info/ageRatingDeclaration": [m.key(age)],
        "appInfos:info/primaryCategory": [m.key(category)],
        "appStoreVersions:version/appStoreVersionLocalizations": [m.key(text)],
        "subscriptionGroups:group/subscriptions": [m.key(sub)], "subscriptions:sub/subscriptionLocalizations": [m.key(subloc)],
    }
    assets = {}
    if media:
        screenshot_set = resource("appScreenshotSets", "set", screenshotDisplayType="APP_IPHONE_67")
        screenshot = resource("appScreenshots", "shot", fileName="screenshot.jpg", fileSize=100)
        resources.extend([screenshot_set, screenshot])
        edges["appStoreVersionLocalizations:version-locale/appScreenshotSets"] = [m.key(screenshot_set)]
        edges["appScreenshotSets:set/appScreenshots"] = [m.key(screenshot)]
        path = directory / "assets" / "shot.png"
        path.parent.mkdir()
        path.write_bytes(b"test PNG contents")
        assets[m.key(screenshot)] = {"path": "assets/shot.png", "sha256": m.digest(path), "fileSize": path.stat().st_size}
    document = {"format": m.FORMAT, "sourceApp": m.linkage(app), "sourceBundleId": "old.bundle", "createdAt": m.now(), "finishedAt": m.now(),
                "complete": True, "resources": {m.key(r): r for r in resources}, "edges": edges, "assets": assets, "errors": []}
    m.save_json(directory / "manifest.json", document)
    m.save_json(directory / "openapi.json", schema)
    return m.Snapshot(directory)


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.snapshot = populate_backup(self.directory)

    def tearDown(self):
        self.temporary.cleanup()

    def quiet(self):
        return contextlib.redirect_stdout(io.StringIO())

    def test_backup_asset_hash_and_corruption(self):
        self.assertEqual(self.snapshot.verify(), [])
        (self.directory / "assets/shot.png").write_bytes(b"changed")
        self.assertIn("SHA-256", self.snapshot.verify()[0])

    def test_path_escape_rejected(self):
        self.snapshot.document["assets"]["appScreenshots:shot"]["path"] = "../outside"
        with self.assertRaisesRegex(RuntimeError, "escapes"):
            self.snapshot.asset(self.snapshot.resources["appScreenshots:shot"])

    def test_interrupted_backup_invalid(self):
        self.snapshot.document.pop("finishedAt")
        self.assertIn("interrupted", self.snapshot.verify()[0])


    def test_schema_filters_immutable_attributes(self):
        schema = m.Schema(small_schema())
        body = schema.payload("apps", "patch", {"name": "read-only", "primaryLocale": "tr", "bundleId": "new"}, resource_id="a")
        self.assertNotIn("name", body["data"]["attributes"])
        self.assertEqual(body["data"]["attributes"]["primaryLocale"], "tr")


    def test_current_app_version_state_field_is_recognized(self):
        self.assertEqual(m.state_of(resource("appStoreVersions", "v", appVersionState="READY_FOR_DISTRIBUTION")), "READY_FOR_DISTRIBUTION")


    def test_image_template_full_resolution(self):
        shot = resource("appScreenshots", "s", imageAsset={"templateUrl": "https://cdn.example/{w}x{h}.{f}", "width": 1290, "height": 2796})
        self.assertEqual(m.asset_location(shot)[0], "https://cdn.example/1290x2796.png")


    def test_pagination_all_pages(self):
        client = object.__new__(m.Client)
        with mock.patch.object(client, "request", side_effect=[{"data": [resource("apps", "1")], "links": {"next": m.API + "/v1/apps?cursor=2"}},
                                                               {"data": [resource("apps", "2")]}]) as req:
            values, _ = client.pages("/v1/apps", {"limit": 200})
        self.assertEqual([v["id"] for v in values], ["1", "2"])
        self.assertEqual(req.call_args_list[1].kwargs["params"], None)


    def test_ecdsa_der_conversion_and_jwt_signature(self):
        private_key = self.directory / "test.p8"
        subprocess.run(["openssl", "genpkey", "-algorithm", "EC", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-out", str(private_key)],
                       check=True, capture_output=True)
        private_key.chmod(0o600)
        m.save_json(self.directory / "profile.json", {"key_id": "KEY", "issuer_id": "ISSUER", "private_key": "test.p8"})
        client = m.Client(self.directory / "profile.json", readonly=True)
        token = client.bearer()
        header, payload, signature = token.split(".")
        decoded = json.loads(base64_decode(payload))
        self.assertEqual(decoded["iss"], "ISSUER")
        self.assertLessEqual(decoded["exp"] - decoded["iat"], 1200)
        self.assertEqual(len(base64_decode(signature)), 64)
        signed = subprocess.run(["openssl", "dgst", "-sha256", "-sign", str(private_key)], input=(header + "." + payload).encode(), capture_output=True, check=True)
        self.assertEqual(len(m.der_signature_to_raw(signed.stdout)), 64)
        with self.assertRaisesRegex(RuntimeError, "read-only"):
            client.request("POST", "/v1/subscriptions", {})
        with self.assertRaisesRegex(RuntimeError, "outside"):
            client.request("GET", "https://example.com/steal")


    def test_archive_traversal_keeps_locales_assets_and_legacy_api_version(self):
        snapshot = self.snapshot
        class Source:
            def request(self, method, path, body=None, params=None):
                if method != "GET":
                    raise AssertionError("Source mutation")
                return {"data": snapshot.root()}
            def pages(self, path, params=None):
                _, _, kind, identity, relation = path.split("/")
                children = snapshot.children(resource(kind, identity), relation)
                if relation == "appScreenshots":
                    children = copy.deepcopy(children)
                    children[0]["attributes"]["imageAsset"] = {"templateUrl": "https://cdn.example/{w}x{h}.{f}", "width": 1290, "height": 2796}
                return children, []
        def media(url, destination):
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(b"new PNG contents")
        output = self.directory / "archive"
        archive = m.Archive(Source(), self.snapshot.schema, output)
        with self.quiet(), mock.patch.object(m, "download", side_effect=media), mock.patch.object(archive, "archive_public_icon"):
            self.assertEqual(archive.backup("source"), 0)
        restored_snapshot = m.Snapshot(output)
        self.assertEqual(restored_snapshot.verify(), [])
        self.assertEqual(restored_snapshot.resources["subscriptionLocalizations:sub-locale"]["_apiVersion"], 1)
        self.assertEqual(restored_snapshot.children(resource("appScreenshotSets", "set"), "appScreenshots")[0]["id"], "shot")
        self.assertEqual(restored_snapshot.resources["appStoreVersionLocalizations:version-locale"]["attributes"]["keywords"], "privacy,browser")


    def test_api_missing_relationship_is_not_silently_marked_complete(self):
        class BrokenSource:
            def request(self, method, path, body=None, params=None):
                return {"data": self_app}
            def pages(self, path, params=None):
                raise m.ApiError(403, "Insufficient permission")
        self_app = self.snapshot.root()
        archive = m.Archive(BrokenSource(), self.snapshot.schema, self.directory / "broken")
        with self.quiet(), contextlib.redirect_stderr(io.StringIO()), mock.patch.object(archive, "archive_public_icon"):
            self.assertEqual(archive.backup("source"), 2)
        document = m.read_json(self.directory / "broken/manifest.json")
        self.assertFalse(document["complete"])
        self.assertTrue(document["errors"])


def base64_decode(value):
    import base64
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


class AppleSchemaTests(unittest.TestCase):
    def setUp(self):
        self.schema = m.schema_at(Path(__file__).resolve().parents[1] / "src/appstore_admin/data/openapi-4.5.json")

    def test_real_legacy_v1_and_new_v2_contracts(self):
        for kind, old_parent, new_parent in [
            ("subscriptionLocalizations", "subscription", "version"),
            ("subscriptionGroupLocalizations", "subscriptionGroup", "version"),
            ("inAppPurchaseLocalizations", "inAppPurchaseV2", "version"),
        ]:
            for version, relationship in [(1, old_parent), (2, new_parent)]:
                body = self.schema.request_schema(kind, "post", version)
                self.assertEqual(body["properties"]["relationships"]["required"], [relationship])

    def test_response_models_select_correct_version(self):
        self.assertEqual(self.schema.response_version("/v1/subscriptions/{id}/subscriptionLocalizations"), 1)
        self.assertEqual(self.schema.response_version("/v1/subscriptionVersions/{id}/localizations"), 2)
        self.assertEqual(self.schema.response_version("/v1/apps/{id}/inAppPurchasesV2"), 2)

    def test_media_commit_contracts(self):
        self.assertIn("sourceFileChecksum", self.schema.request_schema("appScreenshots", "patch", 1)["properties"]["attributes"]["properties"])
        self.assertNotIn("sourceFileChecksum", self.schema.request_schema("subscriptionImages", "patch", 2)["properties"]["attributes"]["properties"])


    def test_app_creation_is_not_available(self):
        self.assertIsNone(self.schema.endpoint("apps", "post"))

    def test_required_create_payloads_match_official_schema(self):
        rel = lambda kind: {"data": {"type": kind, "id": "test"}}
        for kind, version, attrs, relationships in [
            ("appStoreVersions", 1, {"versionString": "1.0", "platform": "IOS", "releaseType": "MANUAL"}, {"app": rel("apps")}),
            ("subscriptions", 1, {"productId": "premium.monthly", "name": "Monthly"}, {"group": rel("subscriptionGroups")}),
            ("inAppPurchases", 2, {"productId": "premium.lifetime", "name": "Lifetime", "inAppPurchaseType": "NON_CONSUMABLE"}, {"app": rel("apps")}),
            ("appScreenshots", 1, {"fileName": "a.png", "fileSize": 5}, {"appScreenshotSet": rel("appScreenshotSets")}),
        ]:
            body = self.schema.payload(kind, "post", attrs, relationships, version=version)
            self.assertEqual(body["data"]["type"], kind)


if __name__ == "__main__":
    unittest.main()
