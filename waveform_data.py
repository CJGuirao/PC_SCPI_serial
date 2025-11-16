import struct
import logging
import json
from datetime import datetime
import numpy as np


class WaveformData:
    """Parse and store waveform data from oscilloscope"""

    def __init__(self):
        self.header = ""
        self.file_length = 0
        self.channels = []
        self.raw_data = b''

    def parse_bin_data(self, data, file_length):
        """Parse binary waveform data according to OWON format"""
        try:
            logging.debug(f"Starting parse_bin_data, data length: {len(data)}")
            self.raw_data = data

            # Parse header (first 6 bytes)
            self.header = data[:6].decode('ascii', errors='ignore')
            logging.info(f"Waveform header: {self.header}")

            self.file_length = file_length

            # Parse channel data
            self.channels = []
            pos = 54  # Start after header and file length

            while pos < len(data):
                logging.debug(f"Parsing channel at position: {pos}")
                if pos + 3 > len(data):
                    logging.warning(f"Insufficient data for channel name at pos {pos}")
                    break

                channel_name = data[pos:pos+3].decode('ascii', errors='ignore')
                logging.debug(f"Channel name: {channel_name}")
                if not channel_name.startswith('CH'):
                    logging.warning(f"Channel name does not start with 'CH': {data[pos-2:pos].hex()} {data[pos:pos+3].hex()} {data[pos+3:pos+5].hex()}")
                    break

                pos += 3

                # Parse block length (4 bytes)
                if pos + 4 > len(data):
                    logging.warning(f"Insufficient data for block length at pos {pos}")
                    break
                block_length = struct.unpack('<i', data[pos:pos+4])[0]
                normal_wave = True
                if block_length < 0:
                    logging.debug(f"Negative block length detected: {block_length}")
                    block_length = abs(block_length)
                    normal_wave = False
                logging.debug(f"Block length: {block_length}")
                pos += 4

                # For normal waves (positive block length)
                if normal_wave:
                    logging.debug(f"Parsing normal wave channel: {channel_name}, block_length: {block_length}")
                    channel_data = self.parse_normal_wave_channel(data, pos, channel_name, block_length, point_width = 2)
                    if channel_data:
                        self.channels.append(channel_data)
                    else:
                        logging.warning(f"Failed to parse normal wave channel: {channel_name}")
                    pos += block_length
                else:
                    # For deep memory waves (negative block length)
                    logging.debug(f"Parsing deep memory wave channel: {channel_name}, block_length: {block_length}")
                    pos += 8  # Skip extended value (4) + offset (4)
                    actual_block_length = block_length + 8
                    channel_data = self.parse_normal_wave_channel(data, pos, channel_name, actual_block_length, point_width = 1)
                    # channel_data = self.parse_normal_wave_channel(data, pos, channel_name, block_length)
                    if channel_data:
                        self.channels.append(channel_data)
                    else:
                        logging.warning(f"Failed to parse deep memory wave channel: {channel_name}")
                    pos += actual_block_length - 20

        except Exception as e:
            logging.error(f"Error parsing waveform data: {e}")

    def parse_normal_wave_channel(self, data, pos, channel_name, block_length, point_width=1):
        """Parse a normal waveform channel"""
        try:
            logging.debug(f"parse_normal_wave_channel: {channel_name} at pos {pos}, block_length {block_length}")
            channel = {
                'name': channel_name,
                'whole_screen_points': 0,
                'num_points': 0,
                'slow_move': 0,
                'timebase_index': 0,
                'zero_point': 0,
                'voltage_index': 0,
                'attenuation': 1,
                'point_interval': 0.0,
                'frequency': 0,
                'cycle': 0,
                'voltage_per_point': 0.0,
                'waveform_data': []
            }

            if pos + 44 <= len(data):
                channel['whole_screen_points'] = struct.unpack('<i', data[pos:pos+4])[0]
                channel['num_points'] = struct.unpack('<i', data[pos+4:pos+8])[0]
                channel['slow_move'] = struct.unpack('<i', data[pos+8:pos+12])[0]
                channel['timebase_index'] = struct.unpack('<i', data[pos+12:pos+16])[0]
                channel['zero_point'] = struct.unpack('<i', data[pos+16:pos+20])[0]
                channel['voltage_index'] = struct.unpack('<i', data[pos+20:pos+24])[0]

                if pos + 28 <= len(data):
                    channel['attenuation'] = struct.unpack('<i', data[pos+24:pos+28])[0]

                if pos + 32 <= len(data):
                    channel['point_interval'] = struct.unpack('<f', data[pos+28:pos+32])[0]

                if pos + 40 <= len(data):
                    channel['frequency'] = struct.unpack('<i', data[pos+32:pos+36])[0]
                    channel['cycle'] = struct.unpack('<i', data[pos+36:pos+40])[0]

                if pos + 44 <= len(data):
                    channel['voltage_per_point'] = struct.unpack('<f', data[pos+40:pos+44])[0]

                data_start = pos + 44
                data_end = data_start + (channel['num_points'] * point_width)
                print(f"Channel {channel_name}: data_start={data_start}, data_end={data_end}, data length={len(data)}, {channel}")
                if data_end <= len(data):
                    waveform = []
                    for i in range(channel['num_points']):
                        data_pos = data_start + (i * point_width)
                        if data_pos + point_width <= len(data):
                            value = struct.unpack('<b', data[data_pos:data_pos+point_width])[0]
                            waveform.append(value)
                    channel['waveform_data'] = waveform
                    logging.info(f"Channel {channel_name}: {len(waveform)} points parsed")

                return channel

        except Exception as e:
            logging.error(f"Error parsing channel {channel_name}: {e}")

        return None

