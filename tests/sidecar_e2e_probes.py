"""E2E fixture: a probe that needs ``pypdf`` — a library the binary lacks.

CI copies this to ``sideinst/probes.py`` and runs it two ways:

  A) with ``NEURALOSD_SIDECAR=off``  — the frozen binary cannot import pypdf,
     so it must exit with an actionable "pip install pypdf" message.
  B) with the sidecar available      — the probe runs in the HOST Python
     (where pypdf is installed) and the binary returns the real answer.

The probe generates its own PDF so the test needs no fixture file.
"""
import os
import tempfile

from neuralosd import probe


@probe(description="Count pages in a freshly generated PDF",
       triggers=["pdf pages", "how many pdf pages", "pdf"])
def pdf_pages():
    from pypdf import PdfReader, PdfWriter

    path = os.path.join(tempfile.mkdtemp(), "t.pdf")
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.add_blank_page(width=200, height=200)
    with open(path, "wb") as fh:
        writer.write(fh)
    pages = len(PdfReader(path).pages)
    return {"pages": pages, "pypdf_pid": os.getpid()}


PROBES = [pdf_pages]
