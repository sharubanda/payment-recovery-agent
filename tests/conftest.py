"""Every test runs against a throwaway SQLite file and with all external services OFF.
The env block runs before any `app.*` import because app/config.py reads env at import."""
import os
import tempfile
from pathlib import Path

_DIR = Path(tempfile.mkdtemp(prefix="pra-tests-"))
os.environ["DATABASE_URL"] = f"sqlite:///{_DIR / 'test.db'}"
for _k in ("RAZORPAY_KEY_ID", "RAZORPAY_KEY_SECRET", "ANTHROPIC_API_KEY", "PRA_FAULTS"):
    os.environ[_k] = ""

import pytest  # noqa: E402

from app import db, faults  # noqa: E402


@pytest.fixture()
def session():
    """A fresh schema per test. Faults are cleared before and after."""
    faults.clear()
    db.configure(f"sqlite:///{_DIR / 'test.db'}")
    from app import models  # noqa: F401
    db.Base.metadata.drop_all(db.engine())
    db.Base.metadata.create_all(db.engine())
    s = db.session()
    try:
        yield s
    finally:
        s.close()
        faults.clear()
