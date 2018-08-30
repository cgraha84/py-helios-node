import multiprocessing
from multiprocessing.managers import (
    BaseManager,
)
import tempfile

import pytest

from hvm.chains.ropsten import ROPSTEN_GENESIS_HEADER, ROPSTEN_NETWORK_ID
from hvm.db.chain import (
    ChainDB,
)

from helios.chains import (
    serve_chaindb,
)
from helios.config import (
    ChainConfig,
)
from helios.db.chain import ChainDBProxy
from helios.db.base import DBProxy
from helios.utils.db import (
    MemoryDB,
)
from helios.utils.ipc import (
    wait_for_ipc,
    kill_process_gracefully,
)


@pytest.fixture
def database_server_ipc_path():
    core_db = MemoryDB()
    core_db[b'key-a'] = b'value-a'

    chaindb = ChainDB(core_db)
    # TODO: use a custom chain class only for testing.
    chaindb.persist_header(ROPSTEN_GENESIS_HEADER)

    with tempfile.TemporaryDirectory() as temp_dir:
        chain_config = ChainConfig(network_id=ROPSTEN_NETWORK_ID, data_dir=temp_dir)

        chaindb_server_process = multiprocessing.Process(
            target=serve_chaindb,
            args=(chain_config, core_db),
        )
        chaindb_server_process.start()

        wait_for_ipc(chain_config.database_ipc_path)

        try:
            yield chain_config.database_ipc_path
        finally:
            kill_process_gracefully(chaindb_server_process)


@pytest.fixture
def manager(database_server_ipc_path):
    class DBManager(BaseManager):
        pass

    DBManager.register('get_db', proxytype=DBProxy)
    DBManager.register('get_chaindb', proxytype=ChainDBProxy)

    _manager = DBManager(address=database_server_ipc_path)
    _manager.connect()
    return _manager


def test_chaindb_over_ipc_manager(manager):
    chaindb = manager.get_chaindb()

    header = chaindb.get_canonical_head()

    assert header == ROPSTEN_GENESIS_HEADER


def test_db_over_ipc_manager(manager):
    db = manager.get_db()

    assert b'key-a' in db
    assert db[b'key-a'] == b'value-a'

    with pytest.raises(KeyError):
        db[b'not-present']
