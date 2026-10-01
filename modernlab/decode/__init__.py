"""Protocol decoders: UART, SPI, I2C, CAN.

Each decoder takes a (times, volts) pair plus parameters and returns a list of
Frame objects.  Nothing here knows about tkinter, matplotlib or instruments.

Usage::

    from modernlab.decode import uart, spi, i2c, can
    frames = uart.decode(times, volts, baud=115200, threshold=1.65)
"""
from modernlab.decode import uart, spi, i2c, can

__all__ = ["uart", "spi", "i2c", "can"]
