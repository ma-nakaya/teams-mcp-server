import json

from teams_mcp.file_markup import generic_file_labels, shared_file_labels
from teams_mcp.parsers import parse_message


def test_generic_attachment_retains_name_but_not_provider_url() -> None:
    content = (
        '<URIObject type="File.1" uri="https://example.invalid/secret">'
        '<OriginalName v="sample &amp; guide.pdf" />'
        '<FileSize v="100" /></URIObject>'
    )
    labels = generic_file_labels(content)
    assert labels == [{"name": "sample & guide.pdf", "type": "file"}]
    message = parse_message(
        {"id": "1", "messagetype": "RichText/Media_GenericFile", "content": content}, "48:notes"
    )
    assert message.attachments == labels
    assert "secret" not in str(message.to_dict())
    deleted = parse_message(
        {"id": "1", "messagetype": "MessageDelete", "content": content}, "48:notes"
    )
    assert deleted.attachments == []


def test_shared_files_from_properties_are_listed_without_query_tokens() -> None:
    files = [
        {
            "fileName": "設計書.docx",
            "fileType": "docx",
            "objectUrl": "https://contoso.sharepoint.com/sites/eng/Shared%20Documents/a.docx?share=tok",
        },
        {
            "fileName": "model.step",
            "fileType": "step",
            "objectUrl": "https://contoso-my.sharepoint.com/personal/u/Documents/model.step#x",
        },
        {"fileName": "elsewhere.pdf", "objectUrl": "https://evil.example/a.pdf"},
        {"fileName": "", "objectUrl": "https://contoso.sharepoint.com/x"},
        "not-a-dict",
    ]
    message = parse_message(
        {
            "id": "1",
            "messagetype": "RichText/Html",
            "content": "<p>see attached</p>",
            "properties": {"files": json.dumps(files)},
        },
        "19:chat",
    )
    assert message.attachments == [
        {
            "name": "設計書.docx",
            "type": "file",
            "file_type": "docx",
            "storage": "sharepoint",
            "url": "https://contoso.sharepoint.com/sites/eng/Shared%20Documents/a.docx",
        },
        {
            "name": "model.step",
            "type": "file",
            "file_type": "step",
            "storage": "onedrive",
            "url": "https://contoso-my.sharepoint.com/personal/u/Documents/model.step",
        },
        {
            "name": "elsewhere.pdf",
            "type": "file",
            "file_type": None,
            "storage": None,
            "url": None,
        },
    ]
    assert "tok" not in str(message.to_dict())
    assert "evil" not in str(message.to_dict())


def test_shared_files_ignore_deleted_and_malformed_values() -> None:
    files = json.dumps([{"fileName": "a.pdf", "objectUrl": "https://c.sharepoint.com/a.pdf"}])
    deleted = parse_message(
        {"id": "1", "messagetype": "MessageDelete", "properties": {"files": files}}, "19:chat"
    )
    assert deleted.attachments == []
    assert shared_file_labels("not json") == []
    assert shared_file_labels({"fileName": "a"}) == []
    assert (
        shared_file_labels([{"fileName": "a", "objectUrl": "http://c.sharepoint.com/a"}])[0]["url"]
        is None
    )


def test_malformed_attachment_urls_keep_the_attachment_without_url() -> None:
    files = [
        {"fileName": "a.pdf", "objectUrl": "https://[bad/a.pdf"},
        {"fileName": "b.pdf", "objectUrl": "https://contoso.sharepoint.com:invalid/b.pdf"},
        {"fileName": "c.pdf", "objectUrl": "https://contoso.sharepoint.com:8443/c.pdf"},
    ]
    labels = shared_file_labels(files)
    assert [item["name"] for item in labels] == ["a.pdf", "b.pdf", "c.pdf"]
    assert all(item["url"] is None for item in labels)


def test_nonfile_and_unclosed_markup_are_not_reported_as_files() -> None:
    assert (
        generic_file_labels('<URIObject type="Picture.1"><OriginalName v="x" /></URIObject>') == []
    )
    assert generic_file_labels('<URIObject type="File.1"><OriginalName v="x" />') == []
