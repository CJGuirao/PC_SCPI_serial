"""Everything that talks to the instrument.

`transport` moves bytes over a link; `capture` turns payloads into volts. Neither
knows about SCPI commands as such, so a dialect can change without touching either.
"""
