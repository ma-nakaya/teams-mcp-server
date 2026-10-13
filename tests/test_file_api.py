from __future__ import annotations

import base64
import json
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable

import httpx
import pytest

from teams_mcp.api import TeamsApi
from teams_mcp.config import Settings
from teams_mcp.errors import ApiError
from teams_mcp.file_api import TeamsFileApi
from teams_mcp.models import CredentialBundle

SELF_UUID = "12345678-1234-1234-1234-123456789abc"
TENANT = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
SELF = f"8:orgid:{SELF_UUID}"
OTHER = "8:orgid:87654321-4321-4321-4321-cba987654321"
OBJECT = "0-test-file_123"
BASE = "https://as-prod.asyncgw.teams.microsoft.com/v1/objects"
CLIENT_ID = "1780000000000"
MESSAGE_ID = "1780000000001"
FILENAME = 'report & "review" <draft>.pdf'
CONTENT = b"%PDF-1.4\nnon-sensitive offline fixture\n%%EOF"
TEAMS_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 "
    "Edg/126.0.0.0 Teams/24165.1410.2974.6689/49"
)


class FakeAuth:
    def __init__(self, identity: object = f"orgid:{SELF_UUID}") -> None:
        claims = base64.urlsafe_b64encode(json.dumps({"skypeid": identity}).encode()).decode()
        self.settings = Settings()
        self.bundle = CredentialBundle(
            refresh_token="unused-offline-refresh",
            client_id="offline-client",
            tenant_id=TENANT,
            skype_token=f"offline.{claims}.fixture",
            skype_expires_at=time.time() + 3600,
            region="apac",
            display_name="Offline test user",
        )

    async def get_bundle(self) -> CredentialBundle:
        return self.bundle


def metadata(**overrides: object) -> dict[str, object]:
    result: dict[str, object] = {
        "id": OBJECT,
        "type": "sharing/file",
        "filename": FILENAME,
        "tenant_id": TENANT,
    }
    result.update(overrides)
    return result


def adapter(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    auth: FakeAuth | None = None,
    maximum: int = 10_000_000,
) -> TeamsFileApi:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)
    api = TeamsApi(
        auth or FakeAuth(),  # type: ignore[arg-type]
        settings=Settings(max_response_bytes=maximum),
        http=http,
    )
    return TeamsFileApi(api)


async def restored(files: TeamsFileApi) -> TeamsFileApi:
    # Stand-in for the root's trusted matching durable confirmed-create receipt.
    await files.restore_created_file(OBJECT, FILENAME)
    return files


def message(**overrides: object) -> dict[str, object]:
    node = ET.Element("URIObject", {"type": "File.1", "uri": f"{BASE}/{OBJECT}"})
    ET.SubElement(node, "OriginalName", {"v": FILENAME})
    result: dict[str, object] = {
        "id": MESSAGE_ID,
        "clientmessageid": CLIENT_ID,
        "messagetype": "RichText/Media_GenericFile",
        "content": ET.tostring(node, encoding="unicode"),
    }
    result.update(overrides)
    return result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "identity", [f"orgid:{SELF_UUID}", SELF, SELF.upper().replace("8:ORGID", "8:orgid")]
)
async def test_create_uses_only_self_permission_and_existing_token(identity: str) -> None:
    auth = FakeAuth(identity)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "POST"
        assert str(request.url) == BASE
        assert request.headers["Authorization"] == f"skype_token {auth.bundle.skype_token}"
        assert request.headers["User-Agent"] == TEAMS_USER_AGENT
        assert request.headers["x-ms-client-version"] == "49/25113001312"
        assert request.headers["Accept"] == "application/json"
        assert "Authentication" not in request.headers
        assert json.loads(request.content) == {
            "type": "sharing/file",
            "filename": FILENAME,
            "permissions": {SELF: ["read"]},
        }
        return httpx.Response(201, json={"id": OBJECT})

    assert await adapter(handler, auth=auth).create_file(FILENAME) == OBJECT
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_upload_requires_exact_acl_and_raw_content_then_readback() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        assert request.headers["User-Agent"] == TEAMS_USER_AGENT
        assert request.headers["x-ms-client-version"] == "49/25113001312"
        if len(paths) == 1:
            assert request.method == "GET"
            assert request.headers["Accept"] == "application/json"
            return httpx.Response(200, json=metadata())
        if len(paths) == 2:
            assert request.method == "PUT"
            assert request.headers["Content-Type"] == "application/octet-stream"
            assert request.headers["Accept"] == "*/*"
            assert request.content == CONTENT
            return httpx.Response(201)
        assert request.method == "GET"
        assert request.headers["Accept"] == "*/*"
        return httpx.Response(200, content=CONTENT)

    files = await restored(adapter(handler))
    await files.upload_file(OBJECT, CONTENT)
    assert await files.read_file(OBJECT) == CONTENT
    assert paths == [
        f"/v1/objects/{OBJECT}",
        f"/v1/objects/{OBJECT}/content/original",
        f"/v1/objects/{OBJECT}/views/original",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad",
    [
        metadata(permissions={SELF: ["read"], OTHER: ["read"]}),
        metadata(permissions={"48:notes": ["read"]}),
        metadata(permissions={SELF: ["read", "write"]}),
        metadata(permissions={}),
        metadata(owner=OTHER),
        metadata(owner=None),
        metadata(creator=OTHER),
    ],
)
async def test_unverified_acl_prevents_binary_upload(bad: dict[str, object]) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=bad)

    with pytest.raises(ApiError) as caught:
        await (await restored(adapter(handler))).upload_file(OBJECT, CONTENT)
    assert caught.value.code == "file_acl_unverified"
    assert [request.method for request in requests] == ["GET"]


@pytest.mark.asyncio
async def test_post_targets_self_and_escapes_xml_without_changing_filename() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(200, json=metadata())
        assert str(request.url) == (
            "https://apac.ng.msg.teams.microsoft.com/v1/users/ME/conversations/48%3Anotes/messages"
        )
        assert request.headers["Authentication"].startswith("skypetoken=")
        assert "Authorization" not in request.headers
        body = json.loads(request.content)
        assert body["clientmessageid"] == CLIENT_ID
        assert body["messagetype"] == "RichText/Media_GenericFile"
        assert body["amsreferences"] == [OBJECT]
        xml = ET.fromstring(body["content"])
        assert xml.tag == "URIObject"
        assert xml.get("uri") == f"{BASE}/{OBJECT}"
        assert xml.find("OriginalName").get("v") == FILENAME  # type: ignore[union-attr]
        assert xml.findtext("Description") == "[approved marker] A < B & C"
        assert xml.find("FileSize").get("v") == str(len(CONTENT))  # type: ignore[union-attr]
        return httpx.Response(201, json={"OriginalArrivalTime": int(MESSAGE_ID)})

    await (await restored(adapter(handler))).post_file(
        OBJECT, FILENAME, len(CONTENT), CLIENT_ID, caption="[approved marker] A < B & C"
    )
    assert [request.method for request in requests] == ["GET", "POST"]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [301, 302, 307, 308, 401, 403, 429, 500, 503])
async def test_failed_mutation_is_never_retried_or_redirected(status: int) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status, headers={"Location": "https://evil.example/secret"})

    with pytest.raises(ApiError) as caught:
        await adapter(handler).create_file(FILENAME)
    assert len(requests) == 1
    assert caught.value.code == (
        "file_redirect_rejected"
        if status < 400
        else "file_operation_ambiguous"
        if status >= 500
        else f"file_http_{status}"
    )
    assert "evil.example" not in str(caught.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("write", [True, False])
async def test_network_error_is_redacted_and_not_retried(write: bool) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        raise httpx.ReadTimeout("sensitive-path-and-token", request=request)

    files = await restored(adapter(handler))
    with pytest.raises(ApiError) as caught:
        if write:
            await files.create_file(FILENAME)
        else:
            await files.read_file(OBJECT)
    assert len(requests) == 1
    assert caught.value.code == ("file_operation_ambiguous" if write else "file_network_error")
    assert "sensitive" not in str(caught.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [b"not json", b"[]", b"{}", b'{"id":"../escape"}'])
async def test_create_unknown_success_is_ambiguous(body: bytes) -> None:
    with pytest.raises(ApiError) as caught:
        await adapter(lambda _: httpx.Response(201, content=body)).create_file(FILENAME)
    assert caught.value.code == "file_operation_ambiguous"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "identity", [None, "", "48:notes", "8:live:someone", f"orgid:{SELF_UUID}/x"]
)
async def test_unknown_identity_is_rejected_before_network(identity: object) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        pytest.fail("Must not call network with an unknown self identity")

    with pytest.raises(ApiError) as caught:
        await adapter(handler, auth=FakeAuth(identity)).create_file(FILENAME)
    assert caught.value.code == "file_identity_invalid"


@pytest.mark.asyncio
async def test_malformed_stored_token_is_rejected_without_exposing_it() -> None:
    auth = FakeAuth()
    auth.bundle.skype_token = "bad.\u3042\u3044\u3046.bad"

    def handler(_: httpx.Request) -> httpx.Response:
        pytest.fail("Malformed token must not reach network")

    with pytest.raises(ApiError) as caught:
        await adapter(handler, auth=auth).create_file(FILENAME)
    assert caught.value.code == "file_identity_invalid"
    assert auth.bundle.skype_token not in str(caught.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "identifier", ["", "..", "https://evil.example", "x/y", "x?secret", "x%2Fy", "x\\y"]
)
async def test_object_id_rejects_path_and_url_injection(identifier: str) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        pytest.fail("Invalid object IDs must not reach network")

    with pytest.raises(ApiError) as caught:
        await adapter(handler).read_file(identifier)
    assert caught.value.code == "file_object_invalid"


@pytest.mark.asyncio
async def test_read_size_cap_and_upload_size_cap() -> None:
    with pytest.raises(ApiError) as caught:
        files = await restored(
            adapter(lambda _: httpx.Response(200, content=b"x" * 11), maximum=10)
        )
        await files.read_file(OBJECT)
    assert caught.value.code == "file_response_too_large"

    def handler(_: httpx.Request) -> httpx.Response:
        pytest.fail("Too-large upload must not reach network")

    with pytest.raises(ApiError) as caught:
        await adapter(handler, maximum=10).upload_file(OBJECT, b"x" * 11)
    assert caught.value.code == "file_content_invalid"


@pytest.mark.asyncio
async def test_find_file_verifies_exact_client_object_filename_and_returns_id() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert "/conversations/48%3Anotes/messages?pageSize=100" in str(request.url)
        return httpx.Response(200, json={"messages": [message()]})

    assert (
        await (await restored(adapter(handler))).find_file(CLIENT_ID, OBJECT, FILENAME)
        == MESSAGE_ID
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "rows",
    [
        [message(messagetype="RichText/Html")],
        [message(id="")],
        [message(content="not xml")],
        [message(content="<!DOCTYPE x><URIObject/>")],
        [message(content='<URIObject type="File.1" uri="https://evil.example"/>')],
        [message(), message()],
    ],
)
async def test_find_file_rejects_mismatches(rows: list[dict[str, object]]) -> None:
    files = await restored(adapter(lambda _: httpx.Response(200, json={"messages": rows})))
    with pytest.raises(ApiError) as caught:
        await files.find_file(CLIENT_ID, OBJECT, FILENAME)
    assert caught.value.code == "file_message_mismatch"


@pytest.mark.asyncio
async def test_find_bounded_absence_returns_none_without_resending() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"messages": [message(clientmessageid="1780000000002")]})

    assert await (await restored(adapter(handler))).find_file(CLIENT_ID, OBJECT, FILENAME) is None
    assert [request.method for request in requests] == ["GET"]


@pytest.mark.asyncio
async def test_created_file_verification_uses_accepted_create_without_claiming_acl_readback() -> (
    None
):
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "POST":
            return httpx.Response(201, json={"id": OBJECT})
        # This is the observed AMS metadata shape: no owner or ACL fields.
        return httpx.Response(200, json=metadata(contents={}, geo="test"))

    files = adapter(handler)
    files.bind_identity(TENANT, SELF)
    identifier = await files.create_file(FILENAME)
    await files.verify_created_file(identifier)
    assert [request.method for request in requests] == ["POST", "GET"]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 202, 204])
async def test_only_201_establishes_creation_provenance(status: int) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status, json={"id": OBJECT})

    files = adapter(handler)
    with pytest.raises(ApiError) as caught:
        await files.create_file(FILENAME)
    assert caught.value.code == "file_operation_ambiguous"
    with pytest.raises(ApiError) as caught:
        await files.read_file(OBJECT)
    assert caught.value.code == "file_provenance_unverified"
    assert [request.method for request in requests] == ["POST"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad",
    [
        metadata(id="another-object"),
        metadata(type="pish/image"),
        metadata(filename="other.pdf"),
        metadata(tenant_id="bbbbbbbb-bbbb-cccc-dddd-eeeeeeeeeeee"),
        metadata(tenant_id=None),
        {"id": OBJECT},
    ],
)
async def test_metadata_mismatch_prevents_upload(bad: dict[str, object]) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=bad)

    files = await restored(adapter(handler))
    with pytest.raises(ApiError) as caught:
        await files.upload_file(OBJECT, CONTENT)
    assert caught.value.code == "file_metadata_mismatch"
    assert [request.method for request in requests] == ["GET"]


async def use_object(files: TeamsFileApi, operation: str) -> object:
    if operation == "verify":
        return await files.verify_created_file(OBJECT)
    if operation == "upload":
        return await files.upload_file(OBJECT, CONTENT)
    if operation == "read":
        return await files.read_file(OBJECT)
    if operation == "post":
        return await files.post_file(OBJECT, FILENAME, len(CONTENT), CLIENT_ID)
    return await files.find_file(CLIENT_ID, OBJECT, FILENAME)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["verify", "upload", "read", "post", "find"])
async def test_unknown_objects_are_rejected_before_network(operation: str) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        pytest.fail("Unknown objects must not reach any service")

    with pytest.raises(ApiError) as caught:
        await use_object(adapter(handler), operation)
    assert caught.value.code == "file_provenance_unverified"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation", ["verify", "upload", "read", "post", "find", "create", "restore"]
)
@pytest.mark.parametrize("changed", ["principal", "tenant"])
async def test_identity_changes_are_rejected_before_network(operation: str, changed: str) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        pytest.fail("Changed identities must not reach any service")

    auth = FakeAuth()
    files = await restored(adapter(handler, auth=auth))
    if changed == "principal":
        auth.bundle = FakeAuth(OTHER).bundle
    else:
        auth.bundle.tenant_id = "bbbbbbbb-bbbb-cccc-dddd-eeeeeeeeeeee"
    with pytest.raises(ApiError) as caught:
        if operation == "create":
            await files.create_file(FILENAME)
        elif operation == "restore":
            await files.restore_created_file(OBJECT, FILENAME)
        else:
            await use_object(files, operation)
    assert caught.value.code == "file_identity_changed"


@pytest.mark.asyncio
async def test_restore_cannot_replace_filename_or_rebind_identity() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        pytest.fail("Restoring provenance does not make network calls")

    files = await restored(adapter(handler))
    await files.restore_created_file(OBJECT, FILENAME)
    with pytest.raises(ApiError) as caught:
        await files.restore_created_file(OBJECT, "other.pdf")
    assert caught.value.code == "file_provenance_unverified"
    with pytest.raises(ApiError) as caught:
        files.bind_identity(TENANT, OTHER)
    assert caught.value.code == "file_identity_changed"


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["post", "find"])
async def test_bound_filename_cannot_change(operation: str) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        pytest.fail("A different filename must not reach any service")

    files = await restored(adapter(handler))
    with pytest.raises(ApiError) as caught:
        if operation == "post":
            await files.post_file(OBJECT, "other.pdf", len(CONTENT), CLIENT_ID)
        else:
            await files.find_file(CLIENT_ID, OBJECT, "other.pdf")
    assert caught.value.code == "file_provenance_unverified"
