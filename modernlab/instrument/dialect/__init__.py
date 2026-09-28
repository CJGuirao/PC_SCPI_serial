"""The command dialects: one module per instrument family.

A dialect owns the node names a family uses and how its replies are spelled, and
nothing else.  It does not know about a link (that is ``transport``), about volts
(that is ``capture``) or about framing and measurement policy (those sit beside
the controller).  A new family is a new module here.
"""
