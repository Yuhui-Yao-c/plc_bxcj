#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""lib/decoders/hmi_decoder.py — HMI binary → raw structured records

职责:
  解析威纶通 MT8000 HMI 二进制文件，输出结构化记录列表。
  不创建 Entity/Relation，不判断变量归属。

输出记录类型:
  - y_address_label: {address, label_text}
  - m0s0i_label: {code, label_text}
  - symbol_binding: {symbol_path, var_name}
  - component: {pos, text, record_type}
"""
from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

MT8000_MAGIC = b'MT8000Series'


class HMIDecoder:
    """威纶通 MT8000 HMI 二进制 → raw record list"""

    Y6_TEXT_PATTERN = re.compile(
        rb'([XY]\d{6})\s+(?:\d+#)?((?:[\xe4-\xe9][\x80-\xbf]{2}){2,30})')
    YDOT_TEXT_PATTERN = re.compile(
        rb'(Y\d+\.\d+)_[A-Z]((?:[\xe4-\xe9][\x80-\xbf]{2}){2,30})')
    SYMBOL_PATTERN = re.compile(
        rb'Application\.Parameter\.([A-Za-z_][A-Za-z_0-9.]{2,60})')
    M0S0I_PATTERN = re.compile(
        rb'(M\d+S\d+I\d+)[_\s]+((?:[\xe4-\xe9][\x80-\xbf]{2}){2,30})')
    BINDING_PATH_PATTERN = re.compile(
        r'(?:CODESYS\s+V3\s+\(Ethernet\)\s*:\s*|Local\s+HMI\s*:\s*)'
        r'(Application\.[^\s]+|LW[\w-]+|RW[\w-]+|Y[\d.]+|X[\d.]+)')

    def decode(self, hmi_path: str) -> dict:
        """Decode HMI binary file into raw records.

        Returns:
            {
                'format': 'weintek_mt8000',
                'source_file': str,
                'records': {
                    'y_address_labels': [{address, label_text}, ...],
                    'm0s0i_labels': [{code, label_text}, ...],
                    'symbol_bindings': [{symbol_path, var_name}, ...],
                    'components': [{pos, text, record_type}, ...],
                },
                'stats': {...},
            }
        """
        path = Path(hmi_path)
        result = {
            'format': 'weintek_mt8000',
            'source_file': str(path),
            'records': {
                'y_address_labels': [],
                'm0s0i_labels': [],
                'symbol_bindings': [],
                'components': [],
            },
            'stats': {},
        }
        if not path.exists():
            return result

        try:
            with open(path, 'rb') as f:
                data = f.read()
        except Exception:
            return result

        if data[:len(MT8000_MAGIC)] != MT8000_MAGIC:
            # Still try regex extraction for non-MT8000 HMI files
            pass

        # Y address labels
        y_mappings = {}
        for m in self.Y6_TEXT_PATTERN.finditer(data):
            addr = m.group(1).decode('ascii')
            text = m.group(2).decode('utf-8', errors='replace')
            if addr not in y_mappings and text not in ('预留', '备用'):
                y_mappings[addr] = text
        for m in self.YDOT_TEXT_PATTERN.finditer(data):
            addr = m.group(1).decode('ascii')
            text = m.group(2).decode('utf-8', errors='replace')
            if addr not in y_mappings and text not in ('预留', '备用'):
                y_mappings[addr] = text
        result['records']['y_address_labels'] = [
            {'address': a, 'label_text': t} for a, t in y_mappings.items()]

        # M0S0I labels
        m0s0i_map = {}
        for m in self.M0S0I_PATTERN.finditer(data):
            code = m.group(1).decode('ascii')
            text = m.group(2).decode('utf-8', errors='replace')
            if code not in m0s0i_map:
                m0s0i_map[code] = text
        result['records']['m0s0i_labels'] = [
            {'code': c, 'label_text': t} for c, t in m0s0i_map.items()]

        # Symbol bindings
        symbols = {m.group(1).decode('ascii', errors='replace')
                    for m in self.SYMBOL_PATTERN.finditer(data)}
        result['records']['symbol_bindings'] = [
            {'symbol_path': s, 'var_name': s.split('.')[-1]} for s in symbols]

        # Structured components (MT8000 length-prefixed records)
        result['records']['components'] = self._scan_components(data)

        result['stats'] = {
            'total_y_labels': len(y_mappings),
            'total_m0s0i_labels': len(m0s0i_map),
            'total_symbol_bindings': len(symbols),
            'total_components': len(result['records']['components']),
        }
        return result

    def _scan_components(self, data: bytes) -> list:
        """Scan MT8000 structured length-prefixed records."""
        records = []
        i = 0
        while i < len(data) - 4:
            length = data[i] | (data[i + 1] << 8)
            if 4 <= length <= 300 and i + 2 + length <= len(data):
                raw = data[i + 2:i + 2 + length]
                try:
                    text = raw.decode('utf-8')
                    if sum(1 for c in text if c.isprintable()) >= length * 0.7:
                        is_binding = 'CODESYS' in text or 'Local HMI' in text
                        has_chinese = any('\u4e00' <= c <= '\u9fff' for c in text)
                        is_m0s0i = bool(re.match(r'M\d+S\d+[IQ]\d+', text))
                        if is_binding and has_chinese:
                            rec_type = 'mixed'
                        elif is_binding:
                            rec_type = 'binding'
                        elif is_m0s0i:
                            rec_type = 'm0s0i'
                        elif has_chinese:
                            rec_type = 'label'
                        else:
                            rec_type = 'other'
                        records.append({
                            'pos': i, 'text': text,
                            'record_type': rec_type,
                        })
                        i += 2 + length
                        continue
                except UnicodeDecodeError:
                    pass
            i += 1
        return records

    @staticmethod
    def find_hmi_project(search_root: str) -> str | None:
        """Find MT8000 HMI project file in a directory tree."""
        root = Path(search_root)
        if not root.exists():
            return None
        for p in root.rglob('project'):
            try:
                with open(p, 'rb') as f:
                    if f.read(len(MT8000_MAGIC)) == MT8000_MAGIC:
                        return str(p)
            except Exception:
                continue
        for p in root.rglob('*.bin'):
            try:
                with open(p, 'rb') as f:
                    if f.read(len(MT8000_MAGIC)) == MT8000_MAGIC:
                        return str(p)
            except Exception:
                continue
        return None
