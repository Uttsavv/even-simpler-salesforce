`esf.testing.record()` now redacts the text of every XML `sessionId` element (the `SessionHeader` of a SOAP call), whatever its format. Before, only values shaped like `00D…!…` were redacted.
