import json
import logging
import re
import struct
from datetime import datetime

import numpy as np


class WaveformData:
    """Parse and store waveform data from oscilloscope captures."""

    def __init__(self):
        self.header = ""
        self.file_length = 0
        self.channels = []
        self.raw_data = b""

    @staticmethod
    def _scale_to_float(value):
        if value is None:
            return None
        if isinstance(value, (int, float)):
            return float(value)
        text = str(value).strip().lower()
        match = re.fullmatch(r"([+-]?(?:\d+(?:\.\d*)?|\.\d+))(?:\s*([a-z/]+))?", text)
        if not match:
            return None
        number = float(match.group(1))
        unit = (match.group(2) or "").strip()
        multipliers = {
            "ns": 1e-9,
            "us": 1e-6,
            "µs": 1e-6,
            "ms": 1e-3,
            "s": 1.0,
            "mv": 1e-3,
            "v": 1.0,
        }
        return number * multipliers.get(unit, 1.0)

    def parse_bin_data(self, data, file_length):
        """Parse legacy OWON waveform data."""
        try:
            logging.debug("Starting parse_bin_data, data length: %s", len(data))
            self.raw_data = data
            self.header = data[:6].decode("ascii", errors="ignore")
            logging.info("Waveform header: %s", self.header)
            self.file_length = file_length
            self.channels = []
            pos = 54

            while pos < len(data):
                if pos + 3 > len(data):
                    break
                channel_name = data[pos:pos + 3].decode("ascii", errors="ignore")
                if not channel_name.startswith("CH"):
                    break
                pos += 3

                if pos + 4 > len(data):
                    break
                block_length = struct.unpack("<i", data[pos:pos + 4])[0]
                normal_wave = block_length >= 0
                block_length = abs(block_length)
                pos += 4

                if normal_wave:
                    channel_data = self._parse_legacy_channel(data, pos, channel_name, block_length, point_width=1)
                    if channel_data:
                        self.channels.append(channel_data)
                    pos += block_length
                else:
                    pos += 8
                    actual_block_length = block_length + 8
                    channel_data = self._parse_legacy_channel(data, pos, channel_name, actual_block_length, point_width=1)
                    if channel_data:
                        self.channels.append(channel_data)
                    pos += actual_block_length - 20
        except Exception as exc:
            logging.error("Error parsing legacy waveform data: %s", exc)

    def _parse_legacy_channel(self, data, pos, channel_name, block_length, point_width=1):
        try:
            channel = {
                "name": channel_name,
                "whole_screen_points": 0,
                "num_points": 0,
                "slow_move": 0,
                "timebase_index": 0,
                "zero_point": 0,
                "voltage_index": 0,
                "attenuation": 1,
                "point_interval": 0.0,
                "frequency": 0,
                "cycle": 0,
                "voltage_per_point": 0.0,
                "waveform_data": [],
            }

            if pos + 44 <= len(data):
                channel["whole_screen_points"] = struct.unpack("<i", data[pos:pos + 4])[0]
                channel["num_points"] = struct.unpack("<i", data[pos + 4:pos + 8])[0]
                channel["slow_move"] = struct.unpack("<i", data[pos + 8:pos + 12])[0]
                channel["timebase_index"] = struct.unpack("<i", data[pos + 12:pos + 16])[0]
                channel["zero_point"] = struct.unpack("<i", data[pos + 16:pos + 20])[0]
                channel["voltage_index"] = struct.unpack("<i", data[pos + 20:pos + 24])[0]
                channel["attenuation"] = struct.unpack("<i", data[pos + 24:pos + 28])[0]
                channel["point_interval"] = struct.unpack("<f", data[pos + 28:pos + 32])[0]
                channel["frequency"] = struct.unpack("<i", data[pos + 32:pos + 36])[0]
                channel["cycle"] = struct.unpack("<i", data[pos + 36:pos + 40])[0]
                channel["voltage_per_point"] = struct.unpack("<f", data[pos + 40:pos + 44])[0]

                data_start = pos + 44
                data_end = data_start + (channel["num_points"] * point_width)
                if data_end <= len(data):
                    waveform = []
                    for i in range(channel["num_points"]):
                        data_pos = data_start + (i * point_width)
                        value = struct.unpack("<b", data[data_pos:data_pos + point_width])[0]
                        waveform.append(value)
                    channel["waveform_data"] = waveform
                    logging.info("Channel %s: %s points parsed", channel_name, len(waveform))
                return channel
        except Exception as exc:
            logging.error("Error parsing channel %s: %s", channel_name, exc)
        return None

    def parse_hds_capture(self, header, channel_payloads):
        """Parse HDS200/HDS300 JSON header plus binary channel payloads."""
        self.raw_data = b""
        self.file_length = sum(len(payload) for payload in channel_payloads.values())
        self.header = json.dumps(header, ensure_ascii=False, sort_keys=True)
        self.channels = []

        timebase_scale = None
        if isinstance(header, dict):
            timebase = header.get("timebase", {})
            timebase_scale = self._scale_to_float(timebase.get("scale"))

        for channel_name, payload in channel_payloads.items():
            channel = self._parse_hds_channel(header, channel_name, payload, timebase_scale)
            if channel:
                self.channels.append(channel)

    def _strip_definite_length(self, payload):
        if len(payload) >= 2 and payload[:1] == b"#" and payload[1:2].isdigit():
            digits = int(payload[1:2].decode("ascii"))
            if len(payload) >= 2 + digits:
                size = int(payload[2:2 + digits].decode("ascii"))
                return payload[2 + digits:2 + digits + size]
        return payload

    def _decode_hds_points(self, payload):
        payload = self._strip_definite_length(payload)
        if len(payload) < 2:
            return []
        usable = len(payload) - (len(payload) % 2)
        points = np.frombuffer(payload[:usable], dtype="<u2")
        if points.size:
            if points.max(initial=0) <= 0x0FFF:
                points = points.astype(np.int32) - 0x0800
            else:
                points = points.astype(np.int16)
        return points.astype(float).tolist()

    def _parse_hds_channel(self, header, channel_name, payload, timebase_scale):
        data = self._decode_hds_points(payload)
        if not data:
            return None

        ch_header = {}
        if isinstance(header, dict):
            channels = header.get("channel", [])
            for item in channels:
                if str(item.get("name", "")).strip().lower() == channel_name.lower():
                    ch_header = item
                    break

        scale_value = self._scale_to_float(ch_header.get("scale")) if ch_header else None
        point_interval = None
        if timebase_scale and len(data):
            point_interval = (timebase_scale * 10.0) / max(len(data) - 1, 1)

        return {
            "name": channel_name.upper(),
            "whole_screen_points": len(data),
            "num_points": len(data),
            "slow_move": int(header.get("sample", {}).get("slowmove", 0)) if isinstance(header, dict) else 0,
            "timebase_index": 0,
            "zero_point": 0,
            "voltage_index": 0,
            "attenuation": ch_header.get("probe", "") if ch_header else "",
            "point_interval": point_interval if point_interval is not None else 1.0,
            "frequency": 0,
            "cycle": 0,
            "voltage_per_point": scale_value if scale_value is not None else 1.0,
            "waveform_data": data,
        }
