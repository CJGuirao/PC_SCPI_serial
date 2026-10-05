"""Compatibility shim: import OWONScopeController from here or directly.

New code should import from modernlab.instrument.controller directly.
This shim exists for the test suite and any external callers.
"""
from modernlab.instrument.controller import OWONScopeController

__all__ = ["OWONScopeController"]
