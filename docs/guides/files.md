# Files

`sf.files` uploads, downloads and shares Salesforce Files. Use it for attachments on records,
such as invoices on an Account.

```python
import even_simpler_salesforce as esf

auth = esf.ClientCredentials(
    domain="https://acme.my.salesforce.com", client_id="3MVG9...", client_secret="..."
)
sf = esf.AsyncSalesforce(auth)

account = await sf.query_one("SELECT Id FROM Account WHERE Name = {}", "Acme")
with open("invoice.pdf", "wb") as f:  # a local file to upload
    f.write(b"%PDF-1.7 ...")

version_id = await sf.files.upload("invoice.pdf", link_to=account["Id"])
print(version_id)  # '068...'
```

Each upload creates a new `ContentVersion` and its `ContentDocument`. Salesforce gives every
file a `ContentDocument` id (`069...`) and every version a `ContentVersion` id (`068...`).
`upload()` returns the version id. You download by version id and share by document id.

## Upload a file

Pass a path, `bytes`, a binary file object or an iterator of `bytes` chunks. With a path or a
file object, the file name comes from the file. Otherwise pass `filename=`. Salesforce takes the
file type from the extension.

```python
report = await sf.files.upload(b"name,amount\nAcme,100\n", filename="q1.csv", title="Q1 report")

with open("invoice.pdf", "rb") as f:
    await sf.files.upload(f, link_to=account["Id"])
```

The client streams every upload. It reads about 256 KiB at a time, so memory use stays flat for
files up to the Salesforce limit of 2 GB.

If the client must resend an upload, it reads a path or a seekable file again. An iterator can
be read only once, so the client never resends it. A token refresh or a connection error then
raises the error instead.

## Share a file with a record

Pass `link_to=` to share the new file with a record in the same request. Salesforce then chooses
the share type and visibility.

To choose them yourself, also pass `share_type=` or `visibility=`. The client then makes two more
calls: one gets the document id and one creates the link. If the link fails, the error message
names the uploaded version id.

```python
await sf.files.upload(
    b"terms", filename="terms.txt", link_to=account["Id"], share_type="V", visibility="AllUsers"
)
```

To share a file that already exists, call `link()` with the document id.

```python
version = await sf.sobject("ContentVersion").get(report, fields=["ContentDocumentId"])
link_id = await sf.files.link(version["ContentDocumentId"], account["Id"], share_type="C")
```

| `share_type` | Access |
|---|---|
| `"V"` | viewer: the user can view the file. Default for `link()` |
| `"C"` | collaborator: the user can view and edit the file |
| `"I"` | inferred: access follows access to the record |

`visibility` is `"AllUsers"`, `"InternalUsers"` or `"SharedUsers"`.

## List the files of a record

`list_for()` returns one `FileLink` per file linked to the record.

```python
for link in await sf.files.list_for(account["Id"]):
    print(link.title, link.file_extension, link.size, link.latest_version_id)
```

It runs one SOQL query on `ContentDocumentLink`. Use `latest_version_id` to download the file.

## Download a file

`download()` streams the content. Use it as a context manager and iterate over the chunks. The
connection closes when the block ends, also if you stop early.

```python
with open("copy.pdf", "wb") as out:
    async with sf.files.download(version_id) as stream:
        async for chunk in stream:
            out.write(chunk)
```

For small files, `download_bytes()` returns the whole content.

```python
data = await sf.files.download_bytes(version_id)
```

## Download a legacy attachment

Older orgs store files as `Attachment` records. `download_attachment()` returns the content of one.

```python
row = await sf.query_one("SELECT Id FROM Attachment WHERE Name = {}", "contract.pdf")
data = await sf.files.download_attachment(row["Id"])
```

## Methods

| Method | Returns | Salesforce call |
|---|---|---|
| `upload(source, *, filename, title, link_to, share_type, visibility)` | ContentVersion id | multipart `POST sobjects/ContentVersion` |
| `download(version_id)` | context manager over the file stream | `GET sobjects/ContentVersion/{id}/VersionData` |
| `download_bytes(version_id)` | `bytes` | same |
| `download_attachment(attachment_id)` | `bytes` | `GET sobjects/Attachment/{id}/Body` |
| `link(document_id, record_id, *, share_type, visibility)` | ContentDocumentLink id | `POST sobjects/ContentDocumentLink` |
| `list_for(record_id)` | `list[FileLink]` | SOQL on `ContentDocumentLink` |

## Test code that uses files

The fakes in `esf.testing` have `files` too. Uploads become records in the fake, and
`file_content()` returns the uploaded bytes.

```python
from even_simpler_salesforce.testing import FakeSalesforce

fake = FakeSalesforce()
account_id = fake.add("Account", {"Name": "Acme"})
version_id = fake.files.upload(b"%PDF", filename="a.pdf", link_to=account_id)
assert fake.file_content(version_id) == b"%PDF"
assert [f.title for f in fake.files.list_for(account_id)] == ["a.pdf"]
```

To seed a file without recording a call, use `fake.add_file(data, filename=..., link_to=...)`.

## How it works

The upload body has two parts. The first part is JSON with `PathOnClient`, `Title` and, with
`link_to=`, `FirstPublishLocationId`. The second part is the file. The client sends
`Content-Length` when it knows the size, and chunked transfer encoding otherwise.

A download emits its `sf.request` event when the stream closes. `bytes_in` is then the number of
bytes read. If the connection fails while you read, iteration raises `TransportError` or
`RequestTimeout`.
