"""Property tests for chunking, including the real sObject Collections chunk sizes.

simple-salesforce regressions this guards against (behaviour only, no code was read or
copied): #590, #693, #739 (batches over the 200-record Collections limit sent in one request,
or records dropped / reordered at chunk boundaries) and #280 (an empty input still sent a
request). The end-to-end request counts are in ``tests/contract/test_collections_chunking.py``.
"""

from __future__ import annotations

import math

from hypothesis import given
from hypothesis import strategies as st

from even_simpler_salesforce.core.records import COLLECTION_READ_CHUNK, COLLECTION_WRITE_CHUNK, chunked

SIZES = st.one_of(
    st.integers(min_value=1, max_value=50), st.sampled_from([COLLECTION_WRITE_CHUNK, COLLECTION_READ_CHUNK])
)


def check(items: list[int], size: int) -> None:
    chunks = [list(c) for c in chunked(items, size)]
    assert [x for c in chunks for x in c] == items  # concatenation == input, in order
    assert all(0 < len(c) <= size for c in chunks)
    assert all(len(c) == size for c in chunks[:-1])  # only the last chunk may be short
    assert len(chunks) == math.ceil(len(items) / size)
    assert (len(chunks) >= 1) == bool(items)


@given(st.lists(st.integers()), SIZES)
def test_chunked_properties(items: list[int], size: int) -> None:
    check(items, size)


@given(st.integers(min_value=0, max_value=4 * COLLECTION_READ_CHUNK + 1))
def test_collection_write_chunks(n: int) -> None:
    check(list(range(n)), COLLECTION_WRITE_CHUNK)


@given(st.integers(min_value=0, max_value=4 * COLLECTION_READ_CHUNK + 1))
def test_collection_read_chunks(n: int) -> None:
    check(list(range(n)), COLLECTION_READ_CHUNK)


def test_documented_limits() -> None:
    # sObject Collections: 200 records per create/update/upsert/delete, 2,000 ids per retrieve.
    # https://developer.salesforce.com/docs/atlas.en-us.api_rest.meta/api_rest/resources_composite_sobjects_collections.htm
    assert COLLECTION_WRITE_CHUNK == 200
    assert COLLECTION_READ_CHUNK == 2000


def test_empty_input_yields_nothing() -> None:
    assert list(chunked([], COLLECTION_WRITE_CHUNK)) == []
