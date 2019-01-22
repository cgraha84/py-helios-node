import asyncio
import logging

import time

import pytest

from typing import cast

from eth_keys import keys
from eth_utils import decode_hex

from helios.dev_tools import create_dev_test_blockchain_database_with_given_transactions, \
    add_transactions_to_blockchain_db
from hvm.db.backends.memory import MemoryDB

from hp2p.consensus import Consensus
from hvm import constants
from hvm import MainnetChain
from hvm.vm.forks.helios_testnet import HeliosTestnetVM

from helios.sync.full.chain import RegularChainSyncer

from tests.helios.core.integration_test_helpers import (
    FakeAsyncMainnetChain,
    FakeAsyncChainDB,
    FakeAsyncAtomicDB,
    get_random_blockchain_db, get_fresh_db,
    FakeMainnetFullNode,
    MockConsensusService,
    get_random_long_time_blockchain_db, get_predefined_blockchain_db)
from tests.helios.core.peer_helpers import (
    get_directly_linked_peers,
    MockPeerPoolWithConnectedPeers,
)
from helios.protocol.common.datastructures import SyncParameters
from hvm.constants import MIN_TIME_BETWEEN_BLOCKS, TIME_BETWEEN_HEAD_HASH_SAVE
from helios.sync.common.constants import (
    FAST_SYNC_STAGE_ID,
    CONSENSUS_MATCH_SYNC_STAGE_ID,
    ADDITIVE_SYNC_STAGE_ID,
    FULLY_SYNCED_STAGE_ID,
)

from helios.dev_tools import create_new_genesis_params_and_state
from tests.integration_test_helpers import (
    ensure_blockchain_databases_identical,
    ensure_chronological_block_hashes_are_identical
)
from queue import Queue

from hvm.constants import random_private_keys
from hvm.chains.mainnet import GENESIS_PRIVATE_KEY

from helios.utils.logging import disable_logging, enable_logging

from logging.handlers import (
    QueueListener,
    QueueHandler,
    RotatingFileHandler,
)
from hvm.tools.logging import (
    TraceLogger,
)

import os
from hp2p.consensus import Consensus
logger = logging.getLogger('helios')


# async def run_with_logging_disabled(awaitable):
#     dummy_queue = Queue()
#     queue_handler = QueueHandler(dummy_queue)
#     queue_handler.setLevel(100)
#
#     logger = cast(TraceLogger, logging.getLogger())
#     logger.addHandler(queue_handler)
#     logger.setLevel(100)
#
#     logger.debug('Logging initialized: PID=%s', os.getpid())
#     logger = logging.getLogger('helios')
#     logger.setLevel(100)
#     logger.debug('AAAAAAAAAAAAa')
#     finished = await awaitable
#     logger.setLevel(0)
#     return finished

@pytest.mark.asyncio
async def _test_consensus_swarm(request, event_loop, bootnode_db, client_db, peer_swarm, validation_function):

    # 0 = bootnode, 1 = client, 2 .... n = peers in swarm
    dbs_for_linking = [bootnode_db, client_db, *peer_swarm]

    # initialize array
    linked_peer_array = []
    for i in range(len(dbs_for_linking)):
        linked_peer_array.append([None]*(len(dbs_for_linking)))

    private_helios_keys = [
        GENESIS_PRIVATE_KEY,
        keys.PrivateKey(random_private_keys[0]),
        *[keys.PrivateKey(random_private_keys[i+1]) for i in range(len(peer_swarm))]
    ]

    # Create all of the linked peers
    for i in range(len(dbs_for_linking)):
        client_db = dbs_for_linking[i]
        client_private_helios_key = private_helios_keys[i]
        for j in range(len(dbs_for_linking)):
            # Don't link it with itself
            if i == j:
                continue

            if linked_peer_array[i][j] is None and linked_peer_array[j][i] is None:
                peer_db = dbs_for_linking[j]
                peer_private_helios_key = private_helios_keys[j]

                client_peer, server_peer = await get_directly_linked_peers(
                    request, event_loop,
                    alice_db=client_db,
                    bob_db=peer_db,
                    alice_private_helios_key=client_private_helios_key,
                    bob_private_helios_key=peer_private_helios_key)

                linked_peer_array[i][j] = client_peer
                linked_peer_array[j][i] = server_peer

    bootstrap_nodes = [linked_peer_array[1][0].remote]

    node_index_to_listen_with_logger = 1
    consensus_services = []
    for i in range(len(dbs_for_linking)):
        if i == 0:
            context = linked_peer_array[i][1].context
            context.chain_config.node_type = 4
            context.chain_config.network_startup_node = True
        else:
            context = linked_peer_array[i][0].context

        peer_pool = MockPeerPoolWithConnectedPeers([x for x in linked_peer_array[i] if x is not None])

        node = FakeMainnetFullNode(dbs_for_linking[i], private_helios_keys[i])

        consensus = Consensus(context=context,
                             peer_pool=peer_pool,
                             bootstrap_nodes=bootstrap_nodes,
                             node=node
                             )

        if i != node_index_to_listen_with_logger:
            # disable logger by renaming it to one we arent listening to
            consensus.logger = logging.getLogger('dummy')
            pass

        consensus_services.append(consensus)


    asyncio.ensure_future(consensus_services[0].run())


    def finalizer():
        event_loop.run_until_complete(asyncio.gather(
            *[x.cancel() for x in consensus_services],
            loop=event_loop,
        ))
        # Yield control so that client/server.run() returns, otherwise asyncio will complain.
        event_loop.run_until_complete(asyncio.sleep(0.1))

    request.addfinalizer(finalizer)

    for i in range(2, len(consensus_services)):
        asyncio.ensure_future(consensus_services[i].run())

    asyncio.ensure_future(consensus_services[1].run())

    await wait_for_consensus_all(consensus_services)

    print("WAITING FUNCTION FIRED")

    await asyncio.sleep(1000)
    await validation_function(consensus_services)


# @pytest.mark.asyncio
# async def test_consensus_root_hash_choice_2(request, event_loop):
#     num_peers_in_swarm = 15
#
#     client_db, server_db = get_fresh_db(), get_predefined_blockchain_db(0)
#
#     peer_dbs = []
#     for i in range(num_peers_in_swarm):
#         peer_dbs.append(MemoryDB(server_db.kv_store.copy()))
#
#     server_node = MainnetChain(server_db, GENESIS_PRIVATE_KEY.public_key.to_canonical_address())
#     server_node.chaindb.initialize_historical_minimum_gas_price_at_genesis(min_gas_price=1, net_tpc_cap=100, tpc=1)
#
#     consensus_root_hash_timestamps = server_node.chain_head_db.get_historical_root_hashes()
#
#     async def validation(consensus_services):
#         client_consensus = consensus_services[1]
#         for timestamp, root_hash in consensus_root_hash_timestamps:
#             client_consensus_choice = await client_consensus.coro_get_root_hash_consensus(timestamp)
#             assert (client_consensus_choice == root_hash)
#
#     await _test_consensus_swarm(request, event_loop, server_db, client_db, peer_dbs, validation)


@pytest.mark.asyncio
async def test_consensus_root_hash_choice_3(request, event_loop):
    num_peers_in_swarm = 15

    base_db = MemoryDB()

    genesis_block_timestamp = int(time.time()/1000)*1000 - 1000*1000


    private_keys = []
    for i in range(len(random_private_keys)):
        private_keys.append(keys.PrivateKey(random_private_keys[i]))

    genesis_params, genesis_state = create_new_genesis_params_and_state(GENESIS_PRIVATE_KEY, int(10000000 * 10 ** 18), genesis_block_timestamp)

    # import genesis block
    MainnetChain.from_genesis(base_db, GENESIS_PRIVATE_KEY.public_key.to_canonical_address(), genesis_params, genesis_state)

    # Client db has only the genesis block
    client_db = MemoryDB(base_db.kv_store.copy())

    tx_list = [
        *[[GENESIS_PRIVATE_KEY, private_keys[i], 1000000-1000*i, genesis_block_timestamp + MIN_TIME_BETWEEN_BLOCKS * i] for i in range(len(random_private_keys))]
    ]

    add_transactions_to_blockchain_db(base_db, tx_list)

    peer_dbs = []
    for i in range(int(num_peers_in_swarm/2)):
        peer_dbs.append(MemoryDB(base_db.kv_store.copy()))

    last_block_timestamp = tx_list[-1][-1]
    additional_tx_list_for_competing_db = [
        [private_keys[4], private_keys[1], 100, last_block_timestamp + MIN_TIME_BETWEEN_BLOCKS * 1],
        [private_keys[4], private_keys[2], 100, last_block_timestamp + MIN_TIME_BETWEEN_BLOCKS * 2],
        [private_keys[4], private_keys[3], 100, last_block_timestamp + MIN_TIME_BETWEEN_BLOCKS * 3],
    ]

    competing_base_db = MemoryDB(base_db.kv_store.copy())
    add_transactions_to_blockchain_db(competing_base_db, additional_tx_list_for_competing_db)

    for i in range(int(num_peers_in_swarm / 2),num_peers_in_swarm):
        peer_dbs.append(MemoryDB(competing_base_db.kv_store.copy()))

    bootstrap_node = MainnetChain(base_db, GENESIS_PRIVATE_KEY.public_key.to_canonical_address())
    bootstrap_node.chaindb.initialize_historical_minimum_gas_price_at_genesis(min_gas_price=1, net_tpc_cap=100, tpc=1)
    consensus_root_hash_timestamps = bootstrap_node.chain_head_db.get_historical_root_hashes()

    async def validation(consensus_services):
        client_consensus = consensus_services[1]
        for timestamp, root_hash in consensus_root_hash_timestamps:
            client_consensus_choice = await client_consensus.coro_get_root_hash_consensus(timestamp)
            assert (client_consensus_choice == root_hash)

    await _test_consensus_swarm(request, event_loop, base_db, client_db, peer_dbs, validation)


# @pytest.mark.asyncio
# async def test_consensus_root_hash_choice_1(request, event_loop):
#     num_peers_in_swarm = 0
#
#     client_db, server_db = get_fresh_db(), get_predefined_blockchain_db(0)
#
#     peer_dbs = []
#     for i in range(num_peers_in_swarm):
#         peer_dbs.append(MemoryDB(server_db.kv_store.copy()))
#
#     server_node = MainnetChain(server_db, GENESIS_PRIVATE_KEY.public_key.to_canonical_address())
#     server_node.chaindb.initialize_historical_minimum_gas_price_at_genesis(min_gas_price=1, net_tpc_cap=100, tpc=1)
#
#     consensus_root_hash_timestamps = server_node.chain_head_db.get_historical_root_hashes()
#
#     async def validation(client_consensus):
#         for timestamp, root_hash in consensus_root_hash_timestamps:
#             client_consensus_choice = await client_consensus.coro_get_root_hash_consensus(timestamp)
#             assert (client_consensus_choice == root_hash)
#
#     await _test_consensus(request, event_loop, server_db, client_db, peer_dbs, validation)
#




@pytest.mark.asyncio
async def _test_consensus(request, event_loop, bootnode_db, client_db, peer_swarm, validation_function):
    client_db = client_db
    server_db = bootnode_db


    client_peer, server_peer = await get_directly_linked_peers(
        request, event_loop,
        alice_db=client_db,
        bob_db=bootnode_db,
        alice_private_helios_key=keys.PrivateKey(random_private_keys[0]),
        bob_private_helios_key=GENESIS_PRIVATE_KEY)


    client_peer_pool = MockPeerPoolWithConnectedPeers([client_peer])

    client_node = FakeMainnetFullNode(client_db, client_peer.context.chain_config.node_private_helios_key)

    client_consensus = Consensus(context = client_peer.context,
                                                 peer_pool = client_peer_pool,
                                                 bootstrap_nodes = [client_peer.remote],
                                                 node = client_node
                                                 )
    client_consensus.logger = logging.getLogger('dummy')

    server_peer_pool = MockPeerPoolWithConnectedPeers([server_peer])

    server_node = FakeMainnetFullNode(server_db, server_peer.context.chain_config.node_private_helios_key)

    server_context = server_peer.context
    server_context.chain_config.node_type = 4
    server_context.chain_config.network_startup_node = True

    server_consensus = Consensus(context=server_peer.context,
                                 peer_pool=server_peer_pool,
                                 bootstrap_nodes=[],
                                 node=server_node
                                 )
    server_consensus.logger = logging.getLogger('dummy')

    asyncio.ensure_future(server_consensus.run())

    def finalizer():
        event_loop.run_until_complete(asyncio.gather(
            client_consensus.cancel(),
            server_consensus.cancel(),
            loop=event_loop,
        ))
        # Yield control so that client/server.run() returns, otherwise asyncio will complain.
        event_loop.run_until_complete(asyncio.sleep(0.1))
    request.addfinalizer(finalizer)


    asyncio.ensure_future(client_consensus.run())


    await wait_for_consensus(server_consensus, client_consensus)

    await asyncio.sleep(1000)
    await validation_function(client_consensus)


# @pytest.mark.asyncio
# async def test_consensus_root_hash_choice(request, event_loop):
#     num_peers_in_swarm = 5
#
#     client_db, server_db = get_fresh_db(), get_predefined_blockchain_db(0)
#
#     peer_dbs = []
#     for i in range(num_peers_in_swarm):
#         peer_dbs.append(server_db.copy())
#
#
#     server_node = MainnetChain(server_db, GENESIS_PRIVATE_KEY.public_key.to_canonical_address())
#     server_node.chaindb.initialize_historical_minimum_gas_price_at_genesis(min_gas_price = 1, net_tpc_cap=100, tpc=1)
#
#     consensus_root_hash_timestamps = server_node.chain_head_db.get_historical_root_hashes()
#
#     async def validation(client_consensus):
#         for timestamp, root_hash in consensus_root_hash_timestamps:
#             client_consensus_choice = await client_consensus.coro_get_root_hash_consensus(timestamp)
#             assert(client_consensus_choice == root_hash)
#
#     await _test_consensus(request, event_loop, server_db, peer_dbs, client_db, validation)







@pytest.fixture
def db_fresh():
    return get_fresh_db()

@pytest.fixture
def db_random():
    return get_random_blockchain_db()

@pytest.fixture
def db_random_long_time(length_in_centiseconds = 25):
    return get_random_long_time_blockchain_db(length_in_centiseconds)


SENDER = keys.PrivateKey(
    decode_hex("49a7b37aa6f6645917e7b807e9d1c00d4fa71f18343b0d4122a4d2df64dd6fee"))
RECEIVER = keys.PrivateKey(
    decode_hex("b71c71a67e1177ad4e901695e1b4b9ee17ae16c6668d313eac2f96dbcda3f291"))
GENESIS_PARAMS = {
    'parent_hash': constants.GENESIS_PARENT_HASH,
    'uncles_hash': constants.EMPTY_UNCLE_HASH,
    'coinbase': constants.ZERO_ADDRESS,
    'transaction_root': constants.BLANK_ROOT_HASH,
    'receipt_root': constants.BLANK_ROOT_HASH,
    'bloom': 0,
    'difficulty': 5,
    'block_number': constants.GENESIS_BLOCK_NUMBER,
    'gas_limit': constants.GENESIS_GAS_LIMIT,
    'gas_used': 0,
    'timestamp': 1514764800,
    'extra_data': constants.GENESIS_EXTRA_DATA,
    'nonce': constants.GENESIS_NONCE
}
GENESIS_STATE = {
    SENDER.public_key.to_canonical_address(): {
        "balance": 100000000000000000,
        "code": b"",
        "nonce": 0,
        "storage": {}
    }
}


class HeliosTestnetVMChain(FakeAsyncMainnetChain):
    vm_configuration = ((0, HeliosTestnetVM),)
    chaindb_class = FakeAsyncChainDB
    network_id = 1

async def wait_for_consensus(server_consensus, client_consensus):
    SYNC_TIMEOUT = 1000

    async def wait_loop():

        while await server_consensus.coro_get_root_hash_consensus(int(time.time())) != await client_consensus.coro_get_root_hash_consensus(int(time.time())):
            server_root_hash = await server_consensus.coro_get_root_hash_consensus(int(time.time()))
            client_root_hash = await client_consensus.coro_get_root_hash_consensus(int(time.time()))
            # print('AAAAAAAAAAAAAA')
            # print(int(time.time()/1000)*1000)
            # print(server_root_hash)
            # print(client_root_hash)
            await asyncio.sleep(1)

    await asyncio.wait_for(wait_loop(), SYNC_TIMEOUT)

async def wait_for_consensus_all(consensus_services):
    SYNC_TIMEOUT = 1000

    async def wait_loop():
        while not all([await consensus_services[0].coro_get_root_hash_consensus(int(time.time())) == await rest.coro_get_root_hash_consensus(int(time.time())) for rest in consensus_services]):
            # server_root_hash = await server_consensus.coro_get_root_hash_consensus(int(time.time()))
            # client_root_hash = await client_consensus.coro_get_root_hash_consensus(int(time.time()))
            print('AAAAAAAAAAAAAA')
            print([await consensus_services[0].coro_get_root_hash_consensus(int(time.time())) == await rest.coro_get_root_hash_consensus(int(time.time())) for rest in consensus_services])
            await asyncio.sleep(1)

    await asyncio.wait_for(wait_loop(), SYNC_TIMEOUT)



# if __name__ == "__main__":
#     __spec__ = 'None'
#     loop = asyncio.get_event_loop()
#     test_regular_syncer(fake_request_object(), loop)