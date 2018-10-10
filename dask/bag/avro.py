import io
import uuid

MAGIC = b'Obj\x01'
SYNC_SIZE = 16


def read_long(fo):
    """variable-length, zig-zag encoding."""
    c = fo.read(1)
    b = ord(c)
    n = b & 0x7F
    shift = 7
    while (b & 0x80) != 0:
        b = ord(fo.read(1))
        n |= (b & 0x7F) << shift
        shift += 7
    return (n >> 1) ^ -(n & 1)


def read_bytes(fo):
    """a long followed by that many bytes of data."""
    size = read_long(fo)
    return fo.read(size)


def read_header(fo):
    """Extract an avro file's header

    fo: file-like
        This should be in bytes mode, e.g., io.BytesIO

    Returns dict representing the header

    Parameters
    ----------
    fo: file-like
    """
    assert fo.read(len(MAGIC)) == MAGIC, 'Magic avro bytes missing'
    meta = {}
    out = {'meta': meta}
    while True:
        n_keys = read_long(fo)
        if n_keys == 0:
            break
        for i in range(n_keys):
            # ignore dtype mapping for bag version
            read_bytes(fo)  # schema keys
            read_bytes(fo)  # schema values
    out['sync'] = fo.read(SYNC_SIZE)
    out['header_size'] = fo.tell()
    fo.seek(0)
    out['head_bytes'] = fo.read(out['header_size'])
    return out


def open_head(fs, path, compression):
    """Open a file just to read its head and size"""
    from dask.bytes.core import OpenFile, logical_size
    with OpenFile(fs, path, compression=compression) as f:
        head = read_header(f)
    size = logical_size(fs, path, compression)
    return head, size


def read_avro(urlpath, blocksize=100000000, storage_options=None,
              compression=None):
    """Read set of avro files

    Use this with arbitrary nested avro schemas. Please refer to the
    fastavro documentation for its capabilities:
    https://github.com/fastavro/fastavro

    Parameters
    ----------
    urlpath: string or list
        Absolute or relative filepath, URL (may include protocols like
        ``s3://``), or globstring pointing to data.
    blocksize: int or None
        Size of chunks in bytes. If None, there will be no chunking and each
        file will become one partition.
    storage_options: dict or None
        passed to backend file-system
    compression: str or None
        Compression format of the targe(s), like 'gzip'. Should only be used
        with blocksize=None.
    """
    from dask.utils import import_required
    from dask import delayed, compute
    from dask.bytes.core import (open_files, get_fs_token_paths,
                                 OpenFile, tokenize)
    from dask.bag import from_delayed
    import_required('fastavro',
                    "fastavro is a required dependency for using "
                    "bag.read_avro().")

    storage_options = storage_options or {}
    if blocksize is not None:
        fs, fs_token, paths = get_fs_token_paths(
            urlpath, mode='rb', storage_options=storage_options)
        dhead = delayed(open_head)
        out = compute(*[dhead(fs, path, compression) for path in paths])
        heads, sizes = zip(*out)
        dread = delayed(read_chunk)

        offsets = []
        lengths = []
        for size in sizes:
            off = list(range(0, size, blocksize))
            length = [blocksize] * len(off)
            offsets.append(off)
            lengths.append(length)

        out = []
        for path, offset, length, head in zip(paths, offsets, lengths, heads):
            delimiter = head['sync']
            f = OpenFile(fs, path, compression=compression)
            token = tokenize(fs_token, delimiter, path, fs.ukey(path),
                             compression, offset)
            keys = ['read-avro-%s-%s' % (o, token) for o in offset]
            values = [dread(f, o, l, head, dask_key_name=key)
                      for o, key, l in zip(offset, keys, length)]
            out.extend(values)

        return from_delayed(out)
    else:
        files = open_files(urlpath, **storage_options)
        dread = delayed(read_file)
        chunks = [dread(fo) for fo in files]
        return from_delayed(chunks)


def read_chunk(fobj, off, l, head):
    """Get rows from raw bytes block"""
    import fastavro
    from dask.bytes.core import read_block
    with fobj as f:
        chunk = read_block(f, off, l, head['sync'])
    head_bytes = head['head_bytes']
    if not chunk.startswith(MAGIC):
        chunk = head_bytes + chunk
    i = io.BytesIO(chunk)
    return list(fastavro.iter_avro(i))


def read_file(fo):
    """Get rows from file-like"""
    import fastavro
    with fo as f:
        return list(fastavro.iter_avro(f))


def to_avro(b, filename, schema, name_function=None, storage_options=None,
            codec='null', sync_interval=16000, metadata=None, compute=True,
            **kwargs):
    """Write bag to set of avro files

    Results in one avro file per input partition.

    Parameters
    ----------
    b: dask.bag.Bag
    filename: list of str or str
        Filenames to write to. If a list, number must match the number of
        partitions. If a string, must includ a glob character "*", which will
        be expanded using name_function
    schema: dict
        Avro schema dictionary, see
        https://fastavro.readthedocs.io/en/latest/writer.html
    name_function: None or callable
        Expands integers into strings, see
        ``dask.bytes.utils.build_name_function``
    storage_options: None or dict
        Extra key/value options to pass to the backend file-system
    codec: 'null', 'deflate', or 'snappy'
        Compression algorithm
    sync_interval: int
        Number of records to include in each block within a file
    metadata: None or dict
        Included in the file header
    compute: bool
        If True, files are written immediately, and function blocks. If False,
        returns delayed objects, which can be computed by the user where
        convenient.
    kwargs: passed to compute(), if compute=True
    """
    # TODO infer schema from first partition of data
    from .core import merge
    from dask.utils import import_required
    from dask import delayed, compute
    from dask.bytes.core import open_files, logical_size
    from dask.bag import from_delayed
    import_required('fastavro',
                    "fastavro is a required dependency for using "
                    "bag.read_avro().")

    storage_options = storage_options or {}
    files = open_files(filename, 'wb', name_function=name_function,
                       num=b.npartitions, **storage_options)
    name = 'to-avro-' + uuid.uuid4().hex
    dsk = {(name, i): (_write_avro_part, (b.name, i), f, schema, codec,
                       sync_interval, metadata)
           for i, f in enumerate(files)}
    out = type(b)(merge(dsk, b.dask), name, b.npartitions)
    if compute:
        out.compute(**kwargs)
        return [f.path for f in files]
    else:
        return out.to_delayed()


def _write_avro_part(part, f, schema, codec, sync_interval, metadata):
    """Create single avro file from list of dictionaries"""
    import fastavro
    with f as f:
        fastavro.writer(f, schema, part, codec, sync_interval, metadata)
