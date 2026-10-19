#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CxobDecoder — Weintek MT8000 Runtime Package Decoder

职责仅限:
  - 解包 .cxob (GZIP)
  - 文件目录
  - component records
  - printable strings
  - symbol references
  - labels
  - address/binding records

禁止在 Decoder 中判断:
  - actor
  - operation
  - Step
  - 中文动作
"""
import gzip
import json
import os
import re
import struct
from pathlib import Path
from collections import defaultdict, Counter


def decode_cxob(cxob_path: str, output_dir: str = None) -> dict:
    """Decode a .cxob Weintek runtime package.

    Args:
        cxob_path: Path to .cxob file
        output_dir: Directory to save extracted files (optional)

    Returns:
        dict with keys:
            - files: list of {path, size, type}
            - strings: list of {offset, text, context_file}
            - inventory: summary statistics
    """
    cxob_path = Path(cxob_path)
    result = {
        'source_file': cxob_path.name,
        'files': [],
        'strings': [],
        'inventory': {},
    }

    # Decompress GZIP
    with gzip.open(str(cxob_path), 'rb') as f:
        raw_data = f.read()

    result['inventory']['decompressed_size'] = len(raw_data)
    result['inventory']['compressed_size'] = cxob_path.stat().st_size

    # Parse embedded directory structure
    # Weintek .cxob contains a directory of files with paths like:
    # ./config, ./mt8000, ./runtime, ./script, etc.
    files = _extract_embedded_files(raw_data)
    result['files'] = files

    # Extract printable strings from each file section
    all_strings = []
    for f_info in files:
        f_path = f_info.get('path', '')
        f_offset = f_info.get('offset', 0)
        f_size = f_info.get('size', 0)
        if f_size <= 0:
            continue
        f_data = raw_data[f_offset:f_offset + f_size]
        strings = _extract_strings(f_data, f_path)
        all_strings.extend(strings)

    result['strings'] = all_strings

    # Build inventory
    result['inventory']['total_files'] = len(files)
    result['inventory']['total_strings'] = len(all_strings)

    # String categories
    chinese_strings = [s for s in all_strings if _has_chinese(s['text'])]
    plc_symbol_strings = [s for s in all_strings if _looks_like_plc_symbol(s['text'])]
    address_strings = [s for s in all_strings if _looks_like_address(s['text'])]

    result['inventory']['chinese_strings'] = len(chinese_strings)
    result['inventory']['plc_symbol_strings'] = len(plc_symbol_strings)
    result['inventory']['address_strings'] = len(address_strings)

    # File type distribution
    type_counts = Counter(f.get('type', 'unknown') for f in files)
    result['inventory']['file_types'] = dict(type_counts)

    # Save extracted files if output_dir provided
    if output_dir:
        out_path = Path(output_dir)
        out_path.mkdir(parents=True, exist_ok=True)

        # Get tarfile reference if available
        tf = getattr(_extract_embedded_files, '_tarfile', None)

        for f_info in files:
            f_path = f_info.get('path', '')
            if not f_path or f_path.startswith('..'):
                continue
            f_size = f_info.get('size', 0)
            if f_size <= 0:
                continue

            # Extract file data from tarfile
            if tf is not None:
                try:
                    # tarfile member name doesn't have ./
                    member_name = f_path.lstrip('./')
                    f_data = tf.extractfile(member_name)
                    if f_data is None:
                        continue
                    f_data = f_data.read()
                except Exception:
                    continue
            else:
                f_offset = f_info.get('offset', 0)
                f_data = raw_data[f_offset:f_offset + f_size]

            dest = out_path / f_path.lstrip('./')
            try:
                if dest.exists() and not dest.is_dir():
                    continue
                dest.parent.mkdir(parents=True, exist_ok=True)
                if f_path.endswith('/'):
                    continue
                with open(dest, 'wb') as f:
                    f.write(f_data)
            except (FileExistsError, OSError):
                continue

        # Save string inventory
        with open(out_path / 'cxob_strings.json', 'w', encoding='utf-8') as f:
            json.dump(all_strings, f, ensure_ascii=False, indent=2)

        # Save file inventory
        with open(out_path / 'cxob_files.json', 'w', encoding='utf-8') as f:
            json.dump(files, f, ensure_ascii=False, indent=2)

    return result


def _extract_embedded_files(data: bytes) -> list:
    """Extract embedded file directory from decompressed .cxob data.

    Weintek .cxob decompressed data is a TAR archive.
    """
    import tarfile
    import io

    files = []
    try:
        tf = tarfile.open(fileobj=io.BytesIO(data), mode='r:')
        for member in tf.getmembers():
            if member.isfile():
                files.append({
                    'path': './' + member.name.lstrip('./'),
                    'size': member.size,
                    'type': _classify_file_type(member.name),
                    'offset': member.offset_data,
                    'tar_member': True,
                })
        # Store tarfile reference for data extraction
        _extract_embedded_files._tarfile = tf
        return files
    except Exception:
        pass

    # Fallback: look for path-like strings as file markers
    path_pattern = re.compile(rb'\./([a-zA-Z0-9_\-./]+)\x00')
    matches = list(path_pattern.finditer(data))
    seen_paths = set()
    for i, m in enumerate(matches):
        path = m.group(0)[:-1].decode('ascii', errors='ignore')
        if path in seen_paths:
            continue
        seen_paths.add(path)
        start = m.end()
        if i + 1 < len(matches):
            end = matches[i + 1].start()
        else:
            end = len(data)
        size = end - start
        files.append({
            'path': path,
            'size': size,
            'type': _classify_file_type(path),
            'offset': m.start(),
        })
    return files


def _classify_file_type(path: str) -> str:
    """Classify file type from path."""
    lower = path.lower()
    if lower.endswith(('.png', '.jpg', '.jpeg', '.bmp', '.gif')):
        return 'image'
    if lower.endswith(('.ttf', '.otf', '.woff')):
        return 'font'
    if lower.endswith(('.csv', '.xlsx')):
        return 'data'
    if lower.endswith(('.json', '.xml')):
        return 'config'
    if lower.endswith(('.bin', '.dat')):
        return 'binary'
    if 'script' in lower:
        return 'script'
    if 'config' in lower:
        return 'config'
    if 'runtime' in lower:
        return 'runtime'
    if 'mt8000' in lower:
        return 'runtime'
    return 'unknown'


def _extract_strings(data: bytes, context: str, min_len: int = 4) -> list:
    """Extract printable strings from binary data.

    Looks for:
    - ASCII strings (>= min_len chars)
    - UTF-16LE strings (Chinese text)
    """
    strings = []

    # ASCII strings
    ascii_pattern = re.compile(rb'[\x20-\x7e]{' + str(min_len).encode() + rb',}')
    for m in ascii_pattern.finditer(data):
        text = m.group(0).decode('ascii', errors='ignore')
        if text.strip():
            strings.append({
                'offset': m.start(),
                'text': text,
                'encoding': 'ascii',
                'context_file': context,
            })

    # UTF-16LE strings (for Chinese text)
    utf16_pattern = re.compile(
        rb'(?:[\x20-\x7e\x00-\x08\x0b\x0c\x0e-\xff][\x00-\xff]){'
        + str(min_len).encode() + rb',}')
    for m in utf16_pattern.finditer(data):
        try:
            text = m.group(0).decode('utf-16-le', errors='ignore')
            if _has_chinese(text) and text.strip():
                strings.append({
                    'offset': m.start(),
                    'text': text,
                    'encoding': 'utf-16-le',
                    'context_file': context,
                })
        except Exception:
            pass

    return strings


def _has_chinese(text: str) -> bool:
    """Check if text contains Chinese characters."""
    for ch in text:
        if '\u4e00' <= ch <= '\u9fff':
            return True
    return False


def _looks_like_plc_symbol(text: str) -> bool:
    """Check if text looks like a PLC symbol/variable name."""
    # Patterns: m_nNextAutoStep, Cylinder[1].HMIAnastole, stWindow[0].bOpen, etc.
    if re.match(r'^[a-zA-Z_]\w*(\[\d+\])?(\.\w+(\[\d+\])?)*$', text):
        return True
    return False


def _looks_like_address(text: str) -> bool:
    """Check if text looks like a PLC address."""
    # Patterns: Y0, X1, D100, M10, %QX0.0, etc.
    if re.match(r'^[YMXDSTFCR]\d+$', text, re.IGNORECASE):
        return True
    if re.match(r'^%[IQM][XBW]\d+(\.\d+)?$', text, re.IGNORECASE):
        return True
    return False


if __name__ == '__main__':
    import sys
    if len(sys.argv) < 2:
        print('Usage: python cxob_decoder.py <cxob_file> [output_dir]')
        sys.exit(1)

    cxob_file = sys.argv[1]
    output = sys.argv[2] if len(sys.argv) > 2 else None

    result = decode_cxob(cxob_file, output)

    print(f'Source: {result["source_file"]}')
    print(f'Decompressed: {result["inventory"]["decompressed_size"]:,} bytes')
    print(f'Files: {result["inventory"]["total_files"]}')
    print(f'Strings: {result["inventory"]["total_strings"]}')
    print(f'  Chinese: {result["inventory"]["chinese_strings"]}')
    print(f'  PLC symbols: {result["inventory"]["plc_symbol_strings"]}')
    print(f'  Addresses: {result["inventory"]["address_strings"]}')
    print(f'File types: {result["inventory"]["file_types"]}')

    if output:
        print(f'\nExtracted to: {output}')
