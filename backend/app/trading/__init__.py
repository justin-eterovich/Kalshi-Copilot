"""Human-in-the-loop trading rail.

Nothing in this package places an order on its own.  Every path into
:mod:`app.trading.executor` starts at a proposal that a human approved
individually, and the interlocks in :mod:`app.trading.interlocks` are checked
again at execution time rather than trusted from the caller.
"""
