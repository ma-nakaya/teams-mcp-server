from teams_mcp.file_markup import generic_file_labels
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


def test_nonfile_and_unclosed_markup_are_not_reported_as_files() -> None:
    assert (
        generic_file_labels('<URIObject type="Picture.1"><OriginalName v="x" /></URIObject>') == []
    )
    assert generic_file_labels('<URIObject type="File.1"><OriginalName v="x" />') == []
