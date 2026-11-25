"""Recompute the Authenticode PE image hash (SHA-256) of a PE32 or PE32+ file.

Read-only. The tool never writes, repairs or signs a file, and it does not read
or verify the signature. It hashes, in order:

1. the headers up to the CheckSum field;
2. the headers after CheckSum up to the Certificate Table data-directory entry;
3. the bytes after that entry up to the end of the last section with
   SizeOfRawData > 0. This includes any filler between the headers and the
   first section, and between sections, and it ignores whether SizeOfHeaders
   is smaller or larger than the first section pointer;
4. the remaining bytes of the file, without the certificate table, followed by
   zeros up to the next multiple of 8 of the file length (or of the certificate
   table offset when there is a table).

Steps 3 and 4 follow what the Windows catalog-admin image hash returned in
tests/pe_authentihash_oracle_check.py and on a corpus of Windows system and
build files. This tool supports only the layouts it checked: it refuses
(error, no digest) fewer than 5 data directories, a PE with no section that has
raw data, overlapping sections, and a certificate table that is not 8-byte
aligned and sized, not complete, or not the last block of the file. These
refusals are limits of this tool. A digest does not mean that Windows accepts
the file: the tool does not validate every header field. In mutations of
synthetic and small system DLL headers, Windows refused files after a change to
SectionAlignment, FileAlignment, or the PointerToRawData of a section whose
SizeOfRawData is 0. Their bytes are hashed; the tool does not validate
their values.
"""
import argparse
import hashlib
import mmap
from pathlib import Path
import struct

CERT_ENTRY = 4  # IMAGE_DIRECTORY_ENTRY_SECURITY


def authentihash(data: "bytes | memoryview") -> str:
    """Return the hex Authenticode SHA-256 digest of the PE image in `data`, or raise ValueError."""
    size = len(data)
    if size < 0x40 or data[:2] != b"MZ":
        raise ValueError("Not a PE file: missing MZ header")
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    if pe < 0x40 or pe + 24 > size or data[pe:pe + 4] != b"PE\0\0":
        raise ValueError("Not a PE file: missing PE signature")
    count, = struct.unpack_from("<H", data, pe + 6)
    optional_size, = struct.unpack_from("<H", data, pe + 20)
    optional = pe + 24
    if optional_size < 2 or optional + optional_size > size:
        raise ValueError("Truncated optional header")
    magic, = struct.unpack_from("<H", data, optional)
    if magic == 0x10B:
        dirs_count_at, dirs_at = 92, 96
    elif magic == 0x20B:
        dirs_count_at, dirs_at = 108, 112
    else:
        raise ValueError(f"Unknown optional header magic 0x{magic:x}")
    if optional_size < dirs_at:
        raise ValueError("Truncated optional header")
    headers_size, = struct.unpack_from("<I", data, optional + 60)
    dirs, = struct.unpack_from("<I", data, optional + dirs_count_at)
    if dirs * 8 > optional_size - dirs_at:
        raise ValueError("Data directories do not fit in the optional header")
    table = optional + optional_size
    table_end = table + 40 * count
    if table_end > size:
        raise ValueError("Truncated section table")
    if headers_size < table_end or headers_size > size:
        raise ValueError("SizeOfHeaders does not cover the headers or exceeds the file")
    checksum_at = optional + 64
    cert_at = optional + dirs_at + 8 * CERT_ENTRY
    if dirs <= CERT_ENTRY:
        raise ValueError("Fewer than 5 data directories: layout not supported by this tool")
    cert_offset, cert_size = struct.unpack_from("<II", data, cert_at)

    sections = []
    for index in range(count):
        _, _, _, raw_size, raw_pointer = struct.unpack_from("<8s4I", data, table + 40 * index)
        if raw_size:
            if raw_pointer < table_end or raw_pointer + raw_size > size:
                raise ValueError(f"Section {index} lies outside the file or inside the section table")
            sections.append((raw_pointer, raw_size))
    if not sections:
        raise ValueError("No section has raw data: layout not supported by this tool")
    sections.sort()
    hashed_end = table_end
    for pointer, raw_size in sections:
        if pointer < hashed_end:
            raise ValueError("Sections overlap: layout not supported by this tool")
        hashed_end = pointer + raw_size

    tail_end = size
    if cert_offset or cert_size:
        if not (cert_offset and cert_size):
            raise ValueError("Certificate Table has an offset or a size but not both: layout not supported by this tool")
        if cert_offset % 8 or cert_size % 8:
            raise ValueError("Certificate Table offset or size is not a multiple of 8: layout not supported by this tool")
        if cert_offset < hashed_end or cert_offset + cert_size != size:
            raise ValueError("Certificate Table is not the last block of the file")
        tail_end = cert_offset

    digest = hashlib.sha256()
    ranges = [(0, checksum_at), (checksum_at + 4, cert_at), (cert_at + 8, hashed_end), (hashed_end, tail_end)]
    for start, end in ranges:
        digest.update(data[start:end])
    digest.update(bytes(-tail_end % 8))
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("image", type=Path)
    args = parser.parse_args()
    try:
        with args.image.open("rb") as stream:
            try:
                mapped = mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ)
            except ValueError:  # an empty file cannot be mapped
                mapped = None
            view = memoryview(mapped) if mapped else memoryview(b"")
            try:
                digest = authentihash(view)
            finally:
                view.release()
                if mapped:
                    mapped.close()
    except (OSError, ValueError) as error:
        parser.exit(1, f"{args.image}: {error}\n")
    print(digest)


if __name__ == "__main__":
    main()
