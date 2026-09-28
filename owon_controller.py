"""Compatibility shim: the controller moved to ``modernlab.instrument``.

Everything that used to live in this file is now split by concern:

    modernlab/instrument/dialect/sds.py    the legacy SDS serial / LAN command set
    modernlab/instrument/dialect/hds.py    the HDS nodes and reply normalisation
    modernlab/instrument/framing.py        letting a framing write land; the ladders
    modernlab/instrument/measurements.py   readings as numbers, qualifiers and all
    modernlab/instrument/controller.py     OWONScopeController, which mixes those

The name below is re-exported so ``from owon_controller import
OWONScopeController`` keeps working for ``main.py``, ``console.py``, the tools and
the tests.  New code should import from :mod:`modernlab.instrument.controller`.

Note on the method-resolution aliases the old module carried: ``legacy_query``,
``legacy_send_command``, ``legacy_query_binary``, ``legacy_read_binary_packet``,
``legacy_download_waveform_data`` and the four trigger aliases, along with
``measure_all_sds``, ``get_all_measurements_sds`` and ``connect_usb_serial``.
They existed to keep a fallback from resolving back to the definition that
replaced it.  With the dialects in separate bases that cannot happen, so they are
gone; the two spellings a caller outside may still reach for --
``connect_usb_serial`` and ``query_serial`` -- are provided by the controller.
"""

from modernlab.instrument.controller import OWONScopeController


class OWONScopeControllerSds(OWONScopeController):
    """Deprecated alias for the controller with the legacy family selected.

    Nothing in the program imports this; it is here because the old file's class
    name was one class for both families and a caller that wanted the serial path
    had only ``OWONScopeController(family="sds")`` to say so.
    """

    def __init__(self, family="sds"):
        super().__init__(family=family)


__all__ = ["OWONScopeController", "OWONScopeControllerSds"]
