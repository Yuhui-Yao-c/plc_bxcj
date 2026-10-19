#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""lib/decoders/__init__.py — Source Decoder package

Decoder 职责:
  raw resource → canonical/raw structured records

Decoder 不判断:
  - 什么是 Step
  - 什么是 actor
  - 什么是 operation
  - 什么是 station

可用 Decoder:
  - PLCopenDecoder  : PLCopen XML → DecodedProgram (Canonical IR + VarDecls)
  - SMC2Decoder     : SMC2 CDATA JSON → DecodedProgram (Canonical IR + VarDecls)
  - HMIDecoder      : HMI binary → raw record list
"""
from .plcopen_decoder import PLCopenDecoder
from .smc2_decoder import SMC2Decoder
from .hmi_decoder import HMIDecoder

__all__ = ['PLCopenDecoder', 'SMC2Decoder', 'HMIDecoder']
