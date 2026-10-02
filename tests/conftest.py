import pytest

from app.storage import Storage


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "test.db"


@pytest.fixture
def storage(db_path):
    return Storage(db_path)
