# Self-chat PDF attachments

`send_chat_file(chat_id, file_path, request_id, confirm=false)` adds an experimental
PDF attachment flow using the existing Teams Skype session and legacy AMS file
API. It requests no new OAuth scope, reads no browser cookies, and creates no
public or organization-wide sharing link.

The first version supports only the authenticated self chat (`48:notes`) and a
regular local PDF. Other chats, channels, network paths, symbolic links, and
reparse points are rejected. The maximum size is the smaller of 10 MiB and
`TEAMS_MAX_RESPONSE_BYTES`; filenames must fit 255 UTF-8 bytes. Paths refer to the
machine running the MCP server, not to an unrelated cloud workspace.

## Send and verify

1. Obtain explicit authorization for the exact file and self destination.
2. Resolve the file locally through its authorized source and verify its filename.
3. Check `teams_auth_status` and the current self chat.
4. Choose one UUID `request_id` and call `send_chat_file` with `confirm=true`.
5. Report success only for `status=sent_and_verified` and
   `attachment_verified=true`.

The existing write enable flag, exact target allowlist, individual confirmation,
and configured marker still apply. Reading and sending do not call any read-state
mutation. AMS creation explicitly requests read access only for the authenticated
user. The adapter binds the accepted creation to that identity, tenant and filename
and checks object metadata before uploading and posting. AMS does not expose an
ACL readback here; accepted creation is the permission evidence. Any contradictory
permission metadata stops the operation. The attachment is compared byte-for-byte
with the local snapshot, and the self message must contain the matching client
message ID, object ID, and filename.

## Uncertain outcomes

Reuse the same UUID after a timeout. Do not create a new UUID to retry an uncertain
send. Each operation writes a local receipt before a network mutation. An existing
receipt permits only delivery reconciliation, never automatic retransmission.
The message lookup is bounded, so absence does not establish non-delivery. A
confirmed HTTP error before posting can leave an empty or unposted private AMS
object; no automatic deletion or cleanup request is made.

Receipts hold hashes and opaque delivery identifiers, not file names, local paths,
PDF content, or tokens. Audit messages record the self destination, byte count,
and digest. File contents and diagnostic response bodies must not be logged or
committed as fixtures.

## Compatibility

AMS is an unsupported internal protocol. Generic files may be rejected or may
render differently in current Teams clients. Verify a nonsensitive self attachment
before using a sensitive file. If the tenant requires a new IC3 or Graph scope,
stop and report the required permission; do not acquire it automatically. Unknown
object metadata, contradictory permissions, redirects, identity mismatches, and
ambiguous mutation failures stop the operation. Mutating requests are never automatically retried.

Wire-format references:

- [SkPy file transfer](https://github.com/Terrance/SkPy/blob/master/skpy/chat.py)
- [SkPy URIObject messages](https://github.com/Terrance/SkPy/blob/master/skpy/msg.py)
- [purple-teams AMS upload](https://github.com/EionRobb/purple-teams/blob/master/teams_messages.c)

The implementation is independently written. These sources establish protocol
details, not a guarantee that a current Teams tenant supports generic files.
