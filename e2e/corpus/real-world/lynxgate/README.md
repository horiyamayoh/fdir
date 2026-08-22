# LynxGate real-input regression corpus

This directory contains four committed ZIP archives used as DOCX/XLSX product
regression inputs. The source manifest pins each archive and each document by
SHA-256 and currently describes 161 documents (60 DOCX and 101 XLSX).

The manifest is a test-input inventory only. Regression tests extract inputs to
a temporary directory, invoke the public converter, and discard generated IR
and conversion metadata when the test completes. No generated report or
representative output is stored in the repository.

The known DOCX XML text-budget rejection remains an expected product limit and
is asserted through its diagnostic; the limit is not relaxed for this corpus.
