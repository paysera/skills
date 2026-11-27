#!/usr/bin/env python3
"""Check that every Paysera Checkout API name the paysera-checkout skill uses exists.

check   offline: compares the skill's code spans and code blocks with checkout-names.json
refresh rebuilds checkout-names.json from the OpenAPI file, the SDK source and the guide markdown
drift   network: compares the snapshot with the public OpenAPI, Packagist and the public guide pages
"""

import argparse
import hashlib
import io
import json
import re
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SNAPSHOT = ROOT / "scripts" / "checkout-names.json"
SKILL_DIR = ROOT / "plugins" / "paysera-checkout" / "skills" / "paysera-checkout"

PUBLIC_OPENAPI = "https://developers.paysera.com/api/checkout-integration-api.txt"
PUBLIC_GUIDES = "https://developers.paysera.com/guides/checkout-modern/"
SDK_PACKAGE = "paysera/lib-checkout-integration-sdk"
PACKAGIST = "https://repo.packagist.org/p2/" + SDK_PACKAGE + ".json"

HTTP_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")
OAUTH2_FIELDS = ["grant_type", "client_id", "client_secret", "access_token", "token_type", "expires_in", "scope"]

KNOWN_WRONG = [
    "order.paid",
    "payment_link.completed",
    "order.created",
    "order.reference_updated",
    "order.status_updated",
    "X-Paysera-Event",
    "processPaymentCallback",
    "UnsupportedCallbackIntegrationException",
    "initiateRefundOrder",
    "buildRefundOrderRequest",
    "buildPaymentOrderCreateRequest",
    "/orders/{id}/refunds",
    "/v1/refund/refund_requests",
]

PHP_ALLOWED_METHODS = {
    "getBody", "getContents", "rewind", "withStatus", "withHeader", "getHeaderLine", "getHeader",
    "getMethod", "write", "createResponse", "createStream", "createServerRequestFromGlobals",
    "fromGlobals", "prepare", "execute", "fetch", "fetchColumn", "beginTransaction", "commit",
    "rollBack", "rowCount", "inTransaction", "format", "setTimezone",
}

CODE_NAME_ALLOWLIST = {
    "hash_equals", "hash_hmac", "json_decode", "json_encode", "file_get_contents", "http_response_code",
    "random_bytes", "bin2hex", "hmac.compare_digest", "hmac.new", "hashlib.sha256", "os.environ",
    "process.env", "crypto.timingSafeEqual", "crypto.createHmac", "crypto.randomUUID", "uuid.uuid4",
    "express.raw", "req.body", "req.headers", "res.status", "request.get_data", "request.headers",
    "php_sapi_name", "getenv", "Buffer.from", "JSON.parse", "JSON.stringify", "str_repeat",
    "success_url", "failure_url", "callback_url", "cancel_url", "redirect_urls",
    "is_test", "callback_id", "idempotency_key", "paysera_order_id", "merchant_order_id",
    "client_secret", "client_id", "grant_type", "access_token",
    "content_type", "text_plain", "application_json", "referer", "retry_after",
}

FENCE = re.compile(r"^(`{3,})\s*([\w+-]*)[^\n]*\n(.*?)^\1\s*$", re.M | re.S)
INLINE = re.compile(r"(?<!`)`([^`\n]+)`(?!`)")
ANNOTATION = re.compile(r"<!--\s*names:\s*(request|response|webhook)\s+(.+?)\s*-->")
METHOD_PATH = re.compile(r"\b(GET|POST|PUT|PATCH|DELETE)\s+(?:https://api\.paysera\.com)?(/[A-Za-z0-9_\-/{}$.]+)")
BARE_PATH = re.compile(r"(?:https://api\.paysera\.com)?(/[a-z-]+/integration/v1/[A-Za-z0-9_\-/{}$]*|/auth/realms/[A-Za-z0-9_\-/]+)")
API_URL = re.compile(r"https://api\.paysera\.com(/[A-Za-z0-9_\-/{}$.]*)")
EVENT = re.compile(r"(?<![\w.:])(order|payment|refund):([a-z_]+)\b")
HEADER = re.compile(r"\b(X-Paysera-[A-Za-z-]+|Idempotency-Key)\b")
PHP_USE = re.compile(r"^\s*use\s+(Paysera\\CheckoutSdk\\[\w\\]+)\s*;", re.M)
PHP_FQCN = re.compile(r"\\?(Paysera\\CheckoutSdk\\[\w\\]+)")
PHP_METHOD = re.compile(r"->\s*(\w+)\s*\(")
PHP_CONST = re.compile(r"\b([A-Z]\w*)::([A-Z][A-Z0-9_]+)\b")
PHP_ARRAY_KEY = re.compile(r"\[\s*'([A-Za-z_][\w]*)'\s*\]|'([a-z][\w]*)'\s*=>")
KEY_ACCESS = re.compile(r"\[\s*[\"']([A-Za-z_][\w]*)[\"']\s*\]")
DOT_CHAIN = re.compile(r"\b(?:body|data|payload|event|order|payment|refund|link|eligibility|created)((?:\.[A-Za-z_][\w]*)+)")
FIELD_LIKE = re.compile(r"^[a-z][a-z0-9]*(?:_[A-Za-z0-9]+)+(?:\.[a-z][A-Za-z0-9_]*)*$|^[a-z][a-z0-9_]*(?:\.[a-z][A-Za-z0-9_]*)+$")
NEGATION = re.compile(r"\b(no|not|never|none|nonexistent|isn't|doesn't|don't|aren't)\b", re.I)
SDK_VERSION = re.compile(r"lib-checkout-integration-sdk[^\n0-9]{0,40}?(\d+\.\d+\.\d+)")
SEMVER = re.compile(r"^\d+\.\d+\.\d+$")


def normalise_path(path):
    path = path.split("?", 1)[0].rstrip(".,;:)")
    path = re.sub(r"\$?\{[^}]*\}", "{}", path)
    path = re.sub(r"/\$\w+", "/{}", path)
    return path.rstrip("/") or "/"


def operation_key(method, path):
    return method.upper() + " " + normalise_path(path)


def flatten_keys(value, prefix=""):
    keys = set()
    if isinstance(value, dict):
        for key, inner in value.items():
            dotted = prefix + key
            keys.add(dotted)
            keys |= flatten_keys(inner, dotted + ".")
    elif isinstance(value, list):
        for inner in value:
            keys |= flatten_keys(inner, prefix)
    return keys


def leaf_names(dotted_keys):
    names = set()
    for key in dotted_keys:
        names.update(key.split("."))
    return names


def load_snapshot(path=SNAPSHOT):
    return json.loads(Path(path).read_text(encoding="utf-8"))


class Truth:
    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.operations = {}
        for group in ("openapi", "guides"):
            for key, spec in snapshot[group]["operations"].items():
                merged = self.operations.setdefault(key, {"request": set(), "response": set()})
                merged["request"].update(spec.get("request", []))
                merged["response"].update(spec.get("response", []))
        for key in snapshot["sdk"]["operations"]:
            self.operations.setdefault(key, {"request": set(), "response": set()})
        self.paths = {key.split(" ", 1)[1] for key in self.operations}
        self.events = {name: set(keys) for name, keys in snapshot["guides"]["events"].items()}
        self.headers = set(snapshot["guides"]["headers"])
        self.fields = set(snapshot["standard_fields"]) | set(snapshot["openapi"].get("values", []))
        for spec in self.operations.values():
            self.fields |= leaf_names(spec["request"]) | leaf_names(spec["response"])
        for keys in self.events.values():
            self.fields |= leaf_names(keys)
        self.sdk_classes = set(snapshot["sdk"]["classes"])
        self.sdk_short = {}
        for fqcn in self.sdk_classes:
            self.sdk_short.setdefault(fqcn.rsplit("\\", 1)[-1], set()).add(fqcn)
        self.sdk_methods = set(snapshot["sdk"]["methods"])
        self.sdk_constants = {k: set(v) for k, v in snapshot["sdk"]["constants"].items()}
        self.sdk_version = snapshot["sdk"]["version"]
        self.known_wrong = snapshot["known_wrong"]


def known_wrong_pattern(name):
    return re.compile(r"(?<![\w-])" + re.escape(name) + r"(?![\w-])")


class Checker:
    def __init__(self, truth):
        self.truth = truth
        self.problems = []
        self.wrong = [(name, known_wrong_pattern(name)) for name in truth.known_wrong]
        self.local_functions = set()

    def report(self, where, line, message):
        self.problems.append("{}:{}: {}".format(where, line, message))

    def check_text(self, text, where):
        self.local_functions |= set(re.findall(r"\bfunction\s+(\w+)\s*\(", text))
        consumed = []
        for match in FENCE.finditer(text):
            start_line = text.count("\n", 0, match.start()) + 2
            lang = match.group(2).lower()
            body = match.group(3)
            annotation = self.annotation_before(text, match.start())
            self.check_block(lang, body, annotation, where, start_line)
            consumed.append((match.start(), match.end()))
        prose = list(text)
        for start, end in consumed:
            for index in range(start, end):
                if prose[index] != "\n":
                    prose[index] = " "
        for number, line in enumerate("".join(prose).splitlines(), 1):
            self.check_prose_line(line, where, number)
        self.check_sdk_version(text, where)

    def annotation_before(self, text, offset):
        previous = text[:offset].rstrip("\n").rsplit("\n", 1)[-1]
        match = ANNOTATION.search(previous)
        return (match.group(1), match.group(2)) if match else None

    def check_prose_line(self, line, where, number):
        negated = bool(NEGATION.search(line))
        for name, pattern in self.wrong:
            if pattern.search(line) and not negated:
                self.report(where, number, "'{}' does not exist; say so only in a sentence that states it".format(name))
        for span in INLINE.findall(line):
            if negated and any(p.search(span) for _, p in self.wrong):
                continue
            self.check_names_in(span, where, number, inline=True)

    def check_block(self, lang, body, annotation, where, start_line):
        for offset, line in enumerate(body.splitlines()):
            number = start_line + offset
            for name, pattern in self.wrong:
                if pattern.search(line):
                    self.report(where, number, "'{}' does not exist".format(name))
            self.check_names_in(line, where, number, inline=False)
            for key in KEY_ACCESS.findall(line):
                self.check_field(key, where, number)
            for chain in DOT_CHAIN.findall(line):
                for segment in chain.strip(".").split("."):
                    self.check_field(segment, where, number, chain=True)
        if lang == "json":
            self.check_json_block(body, annotation, where, start_line)
        elif lang == "php":
            self.check_php_block(body, where, start_line)
        elif lang in ("sh", "bash", "shell", "console"):
            self.check_curl_commands(body, where, start_line)

    def check_names_in(self, text, where, number, inline):
        for method, path in METHOD_PATH.findall(text):
            if operation_key(method, path) not in self.truth.operations:
                self.report(where, number, "unknown operation '{} {}'".format(method, path))
        for path in BARE_PATH.findall(text) + API_URL.findall(text):
            if normalise_path(path) not in self.truth.paths:
                self.report(where, number, "unknown API path '{}'".format(path))
        for kind, name in EVENT.findall(text):
            event = kind + ":" + name
            if event not in self.truth.events:
                self.report(where, number, "unknown webhook event '{}'".format(event))
        for header in HEADER.findall(text):
            if header not in self.truth.headers:
                self.report(where, number, "unknown header '{}'".format(header))
        if inline and FIELD_LIKE.match(text) and text not in CODE_NAME_ALLOWLIST:
            if not re.search(r"\.(md|php|py|js|json|ya?ml|txt|com|net|org)$", text):
                for segment in text.split("."):
                    self.check_field(segment, where, number, chain=True)

    def check_field(self, name, where, number, chain=False):
        if name in self.truth.fields or name in CODE_NAME_ALLOWLIST:
            return
        if name.isupper() or (chain and not re.match(r"^[a-z][a-z0-9]*(?:_[A-Za-z0-9]+)*$", name)):
            return
        if chain and "_" not in name and name.islower():
            return
        self.report(where, number, "unknown field '{}'".format(name))

    def keys_for(self, annotation):
        kind, target = annotation
        if kind == "webhook":
            return self.truth.events.get(target.strip())
        parts = target.split(None, 1)
        if len(parts) != 2:
            return None
        spec = self.truth.operations.get(operation_key(parts[0], parts[1]))
        return spec[kind] if spec else None

    def check_json_block(self, body, annotation, where, start_line):
        if annotation is None:
            self.report(where, start_line - 1, "json block needs a '<!-- names: request|response|webhook ... -->' line above it")
            return
        allowed = self.keys_for(annotation)
        if allowed is None:
            self.report(where, start_line - 1, "annotation '{} {}' names nothing known".format(*annotation))
            return
        try:
            value = json.loads(body)
        except ValueError as error:
            self.report(where, start_line, "json block does not parse: {}".format(error))
            return
        for key in sorted(flatten_keys(value)):
            if not key_allowed(key, allowed):
                self.report(where, start_line, "'{}' is not a field of {} {}".format(key, *annotation))

    def check_php_block(self, body, where, start_line):
        imported = {}
        for fqcn in PHP_USE.findall(body):
            imported[fqcn.rsplit("\\", 1)[-1]] = fqcn
        for offset, line in enumerate(body.splitlines()):
            number = start_line + offset
            for fqcn in PHP_FQCN.findall(line):
                if fqcn not in self.truth.sdk_classes:
                    self.report(where, number, "unknown SDK class '{}'".format(fqcn))
            for method in PHP_METHOD.findall(line):
                if method not in self.truth.sdk_methods | PHP_ALLOWED_METHODS | self.local_functions:
                    self.report(where, number, "'{}()' is neither an SDK method nor allow-listed".format(method))
            for short, constant in PHP_CONST.findall(line):
                fqcn = imported.get(short)
                if fqcn is None or constant == "class":
                    continue
                if constant not in self.truth.sdk_constants.get(fqcn, set()):
                    self.report(where, number, "unknown constant '{}::{}'".format(short, constant))
            for pair in PHP_ARRAY_KEY.findall(line):
                key = pair[0] or pair[1]
                self.check_field(key, where, number)

    def check_curl_commands(self, body, where, start_line):
        joined = re.sub(r"\\\n", " ", body)
        for command in re.finditer(r"curl\b[^\n]*", joined):
            text = command.group(0)
            number = start_line + body[: max(0, body.find("curl", command.start() - 2))].count("\n")
            method = re.search(r"-X\s+([A-Z]+)", text)
            url = API_URL.search(text)
            data = re.search(r"(?:-d|--data(?:-raw)?)\s+'(\{.*?\})'", text)
            if not (url and data):
                continue
            key = operation_key(method.group(1) if method else "POST", url.group(1))
            spec = self.truth.operations.get(key)
            if spec is None:
                continue
            try:
                value = json.loads(data.group(1))
            except ValueError as error:
                self.report(where, number, "curl body does not parse: {}".format(error))
                continue
            for field in sorted(flatten_keys(value)):
                if not key_allowed(field, spec["request"]):
                    self.report(where, number, "'{}' is not a request field of {}".format(field, key))

    def check_sdk_version(self, text, where):
        for match in SDK_VERSION.finditer(text):
            if match.group(1) != self.truth.sdk_version:
                number = text.count("\n", 0, match.start()) + 1
                self.report(where, number, "SDK version {} differs from the snapshot's {}".format(match.group(1), self.truth.sdk_version))


def check_skill(snapshot, skill_dir=SKILL_DIR):
    checker = Checker(Truth(snapshot))
    files = sorted(Path(skill_dir).rglob("*.md"))
    if not files:
        return ["{}: no markdown files found".format(skill_dir)]
    skill_md = Path(skill_dir) / "SKILL.md"
    if skill_md.is_file() and not SDK_VERSION.search(skill_md.read_text(encoding="utf-8")):
        checker.problems.append("SKILL.md does not state the targeted lib-checkout-integration-sdk version")
    for path in files:
        checker.local_functions |= set(re.findall(r"\bfunction\s+(\w+)\s*\(", path.read_text(encoding="utf-8")))
    for path in files:
        try:
            where = str(path.relative_to(ROOT))
        except ValueError:
            where = str(path)
        checker.check_text(path.read_text(encoding="utf-8"), where)
    return checker.problems


def schema_keys(schema, spec, prefix="", depth=0):
    if depth > 12 or not isinstance(schema, dict):
        return set()
    if "$ref" in schema:
        target = spec
        for part in schema["$ref"].lstrip("#/").split("/"):
            target = target.get(part, {})
        return schema_keys(target, spec, prefix, depth + 1)
    keys = set()
    for combinator in ("allOf", "oneOf", "anyOf"):
        for inner in schema.get(combinator, []):
            keys |= schema_keys(inner, spec, prefix, depth + 1)
    if schema.get("type") == "array" or "items" in schema:
        keys |= schema_keys(schema.get("items", {}), spec, prefix, depth + 1)
    if schema.get("additionalProperties") and prefix:
        keys.add(prefix + "*")
    for name, inner in (schema.get("properties") or {}).items():
        keys.add(prefix + name)
        keys |= schema_keys(inner, spec, prefix + name + ".", depth + 1)
    return keys


def key_allowed(key, allowed):
    if key in allowed:
        return True
    parts = key.split(".")
    return any(".".join(parts[:i]) + ".*" in allowed for i in range(1, len(parts)))


def enum_values(node):
    values = set()
    if isinstance(node, dict):
        for key, inner in node.items():
            if key == "enum" and isinstance(inner, list):
                values.update(v for v in inner if isinstance(v, str))
            else:
                values |= enum_values(inner)
    elif isinstance(node, list):
        for inner in node:
            values |= enum_values(inner)
    return values


def openapi_operations(spec):
    operations = {}
    for path, item in (spec.get("paths") or {}).items():
        for method, operation in item.items():
            if method.upper() not in HTTP_METHODS:
                continue
            request = set()
            for media in ((operation.get("requestBody") or {}).get("content") or {}).values():
                request |= schema_keys(media.get("schema", {}), spec)
            response = set()
            for code, answer in (operation.get("responses") or {}).items():
                if not str(code).startswith("2"):
                    continue
                for media in ((answer or {}).get("content") or {}).values():
                    response |= schema_keys(media.get("schema", {}), spec)
            operations[operation_key(method, path)] = {"request": sorted(request), "response": sorted(response)}
    return operations


def markdown_sections(text):
    sections = []
    current = {"title": "", "level": 0, "body": []}
    in_fence = False
    for line in text.splitlines():
        if line.startswith("```"):
            in_fence = not in_fence
        heading = re.match(r"^(#{1,6})\s+(.*)$", line) if not in_fence else None
        if heading:
            sections.append(current)
            current = {"title": heading.group(2).strip(), "level": len(heading.group(1)), "body": []}
        else:
            current["body"].append(line)
    sections.append(current)
    return [dict(s, body="\n".join(s["body"])) for s in sections]


def table_first_column(body):
    names = []
    for line in body.splitlines():
        cell = re.match(r"^\|\s*`([^`]+)`\s*\|", line)
        if cell:
            names.append(cell.group(1))
    return names


def first_json(body):
    for match in FENCE.finditer(body + "\n"):
        if match.group(2).lower() == "json":
            try:
                return json.loads(match.group(3))
            except ValueError:
                continue
    return None


def guide_page(path, guides_dir):
    return PUBLIC_GUIDES + str(Path(path).relative_to(guides_dir).with_suffix("")).replace("\\", "/")


def curl_body_keys(body):
    curl = re.search(r"-d\s+'(\{.*?\})'", body, re.S)
    if not curl:
        return set()
    return flatten_keys(json.loads(curl.group(1)))


def section_fields(section):
    title = section["title"].lower()
    body = section["body"]
    request, response = set(), set()
    if "request parameters" in title:
        request.update(table_first_column(body))
    if title.startswith("response") and "code" not in title:
        response |= flatten_keys(first_json(body) or {})
        response.update(name for name in table_first_column(body) if re.match(r"^[a-z_][\w.]*$", name))
    if title == "request" or title.startswith("request body"):
        request |= flatten_keys(first_json(body) or {}) | curl_body_keys(body)
    return request, response


def guide_operations(guides_dir):
    operations = {}
    for path in sorted((Path(guides_dir) / "api-integration").glob("*.md")):
        current = None
        for section in markdown_sections(path.read_text(encoding="utf-8")):
            declared = re.search(r"^```\s*\n(GET|POST|PUT|PATCH|DELETE) (/\S+)\n```", section["body"], re.M)
            if declared:
                current = operation_key(declared.group(1), declared.group(2))
                operations.setdefault(current, {"request": set(), "response": set(), "page": guide_page(path, guides_dir)})
            if current is None:
                continue
            request, response = section_fields(section)
            operations[current]["request"] |= request
            operations[current]["response"] |= response
    return {k: {"request": sorted(v["request"]), "response": sorted(v["response"]), "page": v["page"]} for k, v in operations.items()}


def guide_events(guides_dir):
    path = Path(guides_dir) / "reference" / "webhook-events.md"
    text = path.read_text(encoding="utf-8")
    page = guide_page(path, guides_dir)
    sections = {s["title"]: s["body"] for s in markdown_sections(text)}
    events = {}
    for line in sections.get("Events", "").splitlines():
        row = re.match(r"^\|\s*`(\w+)`\s*\|\s*`(\w+)`\s*\|", line)
        if row:
            events[row.group(1) + ":" + row.group(2)] = set()
    snapshot = first_json(sections.get("Order Snapshot Event", "")) or {}
    thin = first_json(sections.get("Thin Payment Event", "")) or {}
    cancelled = first_json(sections.get("Order Canceled Event", "")) or {}
    events["order:amount_paid_updated"] = flatten_keys(snapshot)
    events["payment:status_updated"] = flatten_keys(thin)
    refund_line = re.search(r"`refund` block instead of `payment`: (.+)", sections.get("Thin Payment Event", ""))
    refund_fields = re.findall(r"`(\w+)`", refund_line.group(1)) if refund_line else []
    events["refund:status_updated"] = {k for k in flatten_keys(thin) if not k.startswith("payment")} | {"refund"} | {
        "refund." + name for name in refund_fields
    }
    canceled_fields = {name for name in table_first_column(sections.get("Order Canceled Event", "")) if name.startswith("order.")}
    events["order:status_updated"] = flatten_keys(cancelled) | canceled_fields
    return {name: {"keys": sorted(keys), "page": page} for name, keys in events.items()}


def guide_headers(guides_dir):
    path = Path(guides_dir) / "api-integration" / "webhooks.md"
    headers = {}
    for section in markdown_sections(path.read_text(encoding="utf-8")):
        if section["title"] == "Headers":
            for name in table_first_column(section["body"]):
                headers[name] = guide_page(path, guides_dir)
    refunds = Path(guides_dir) / "api-integration" / "refunds.md"
    for section in markdown_sections(refunds.read_text(encoding="utf-8")):
        if section["title"] == "Headers":
            for name in table_first_column(section["body"]):
                headers[name] = guide_page(refunds, guides_dir)
    return headers


def sdk_names(sdk_dir):
    sdk_dir = Path(sdk_dir)
    classes, methods, constants, operations = set(), set(), {}, set()
    for path in sorted((sdk_dir / "src").rglob("*.php")):
        text = path.read_text(encoding="utf-8")
        namespace = re.search(r"^namespace\s+([\w\\]+);", text, re.M)
        declared = re.search(r"^(?:final\s+|abstract\s+)*(?:class|interface|trait)\s+(\w+)", text, re.M)
        if not (namespace and declared):
            continue
        fqcn = namespace.group(1) + "\\" + declared.group(1)
        classes.add(fqcn)
        methods.update(re.findall(r"public\s+(?:static\s+)?function\s+(\w+)", text))
        names = re.findall(r"(?:public\s+)?const\s+([A-Z][A-Z0-9_]*)\s*=", text)
        if names:
            constants[fqcn] = sorted(set(names))
        values = re.findall(r"_PATH\s*=\s*'([^']+)'", text) if path.name == "PaymentApiUrlProvider.php" else []
        for value in values:
            for method in ("GET", "POST"):
                operations.add(operation_key(method, value))
    methods.discard("__construct")
    composer = json.loads((sdk_dir / "composer.json").read_text(encoding="utf-8"))
    return {
        "classes": sorted(classes),
        "methods": sorted(methods),
        "constants": constants,
        "operations": sorted(operations),
        "version": composer.get("version", ""),
    }


def read_source(location):
    if re.match(r"^https?://", location):
        request = urllib.request.Request(location, headers={"User-Agent": "paysera-skills-name-check"})
        with urllib.request.urlopen(request, timeout=60) as answer:
            return answer.read()
    return Path(location).read_bytes()


def parse_yaml(raw):
    import yaml

    return yaml.safe_load(raw)


def packagist_versions():
    data = json.loads(read_source(PACKAGIST))
    return data["packages"][SDK_PACKAGE]


def download_sdk(version, target):
    for entry in packagist_versions():
        if entry["version"] == version:
            archive = zipfile.ZipFile(io.BytesIO(read_source(entry["dist"]["url"])))
            archive.extractall(target)
            roots = sorted(p for p in Path(target).iterdir() if p.is_dir())
            return (roots[0] if roots else Path(target)), entry["dist"]["reference"]
    raise SystemExit("version {} is not on Packagist".format(version))


def refresh(args):
    raw = read_source(args.openapi)
    sdk = sdk_names(args.sdk)
    version = args.sdk_version or sdk.pop("version")
    sdk.pop("version", None)
    if not SEMVER.match(version):
        raise SystemExit("cannot tell the SDK version; pass --sdk-version")
    snapshot = {
        "sdk": dict(
            package=SDK_PACKAGE,
            version=version,
            dist_reference=args.dist_reference,
            source="https://github.com/paysera/lib-checkout-integration-sdk",
            **sdk
        ),
        "openapi": {
            "source": PUBLIC_OPENAPI,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "operations": openapi_operations(parse_yaml(raw)),
            "values": sorted(enum_values(parse_yaml(raw))),
        },
        "guides": {
            "source": PUBLIC_GUIDES,
            "operations": guide_operations(args.guides),
            "events": {k: v["keys"] for k, v in guide_events(args.guides).items()},
            "event_pages": {k: v["page"] for k, v in guide_events(args.guides).items()},
            "headers": sorted(guide_headers(args.guides)),
            "header_pages": guide_headers(args.guides),
        },
        "standard_fields": OAUTH2_FIELDS,
        "known_wrong": KNOWN_WRONG,
    }
    Path(args.output).write_text(json.dumps(snapshot, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print("wrote {}".format(args.output))
    return 0


def openapi_drift(snapshot):
    live = openapi_operations(parse_yaml(read_source(PUBLIC_OPENAPI)))
    known = snapshot["openapi"]["operations"]
    if live == known:
        return []
    added = sorted(set(live) - set(known))
    removed = sorted(set(known) - set(live))
    return ["public OpenAPI changed: added {} removed {} (or fields changed)".format(added, removed)]


def sdk_drift(snapshot):
    import tempfile

    problems = []
    versions = [entry["version"] for entry in packagist_versions() if SEMVER.match(entry["version"])]
    newest = max(versions, key=lambda v: tuple(int(p) for p in v.split(".")))
    pinned = snapshot["sdk"]["version"]
    if newest != pinned:
        problems.append("Packagist has {} {}, the skill targets {}".format(SDK_PACKAGE, newest, pinned))
    with tempfile.TemporaryDirectory() as scratch:
        root, reference = download_sdk(pinned, scratch)
        names = sdk_names(root)
    if reference != snapshot["sdk"]["dist_reference"]:
        problems.append("Packagist dist reference for {} changed: {}".format(pinned, reference))
    changed = [f for f in ("classes", "methods", "constants", "operations") if names[f] != snapshot["sdk"][f]]
    if changed:
        problems.append("SDK {} {} differ from the snapshot".format(pinned, ", ".join(changed)))
    return problems


def guide_expectations(snapshot):
    guides = snapshot["guides"]
    for name, page in guides["event_pages"].items():
        yield "event", name, page, name.split(":", 1)[-1]
    for name, page in guides["header_pages"].items():
        yield "header", name, page, name
    for key, spec in guides["operations"].items():
        yield "operation", key, spec["page"], key.split(" ", 1)[1].split("{}")[0].rstrip("/")


def guides_drift(snapshot):
    pages, problems = {}, []
    for kind, name, page, needle in guide_expectations(snapshot):
        if page not in pages:
            pages[page] = read_source(page).decode("utf-8", "replace")
        if needle not in pages[page]:
            problems.append("{} '{}' no longer appears on {}".format(kind, name, page))
    return problems


def drift(args):
    snapshot = load_snapshot(args.snapshot)
    problems = openapi_drift(snapshot) + sdk_drift(snapshot) + guides_drift(snapshot)
    for problem in problems:
        print("DRIFT " + problem, file=sys.stderr)
    if not problems:
        print("snapshot matches the public OpenAPI, Packagist and guide pages")
    return 1 if problems else 0


def check(args):
    problems = check_skill(load_snapshot(args.snapshot), args.skill_dir)
    for problem in problems:
        print("ERROR " + problem, file=sys.stderr)
    if problems:
        return 1
    print("every Checkout name in the skill is in the snapshot")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command")
    check_parser = sub.add_parser("check")
    check_parser.add_argument("--snapshot", default=str(SNAPSHOT))
    check_parser.add_argument("--skill-dir", default=str(SKILL_DIR))
    refresh_parser = sub.add_parser("refresh")
    refresh_parser.add_argument("--openapi", default=PUBLIC_OPENAPI)
    refresh_parser.add_argument("--sdk", required=True)
    refresh_parser.add_argument("--sdk-version")
    refresh_parser.add_argument("--dist-reference", required=True)
    refresh_parser.add_argument("--guides", required=True)
    refresh_parser.add_argument("--output", default=str(SNAPSHOT))
    drift_parser = sub.add_parser("drift")
    drift_parser.add_argument("--snapshot", default=str(SNAPSHOT))
    args = parser.parse_args(argv)
    if args.command in (None, "check"):
        if args.command is None:
            args = check_parser.parse_args([])
        return check(args)
    return {"refresh": refresh, "drift": drift}[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
