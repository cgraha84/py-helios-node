from cytoolz import (
    identity,
)
from eth_typing import Hash32, Address
from eth_utils import (
    decode_hex,
    encode_hex,
    int_to_big_endian,
    is_integer,
    big_endian_to_int,
    to_wei,
    from_wei,
)

from eth_keys import keys
import time
from hvm.rlp.transactions import BaseReceiveTransaction
from helios.exceptions import BaseRPCError
from helios.rpc.constants import MAX_ALLOWED_AGE_OF_NEW_RPC_BLOCK, MAX_ALLOWED_LENGTH_BLOCK_IMPORT_QUEUE
from helios.rpc.format import (
    block_to_dict,
    header_to_dict,
    format_params,
    to_int_if_hex,
    transaction_to_dict,
    receipt_to_dict,
    receive_transactions_to_dict,
    decode_hex_if_str,
    receive_transaction_to_dict,
    connected_nodes_to_dict,
    normalize_transaction_dict,)
import rlp_cython as rlp
from helios.sync.common.constants import FULLY_SYNCED_STAGE_ID

from hvm.exceptions import (
    CanonicalHeadNotFound,
    HeaderNotFound,
    TransactionNotFound,
)
from hvm.utils.blocks import does_block_meet_min_gas_price, get_block_average_transaction_gas_price

from hvm.types import Timestamp

#from hp2p.chain import NewBlockQueueItem

from eth_utils import is_hex_address, to_checksum_address

# Tell mypy to ignore this import as a workaround for https://github.com/python/mypy/issues/4049
from helios.rpc.modules import (  # type: ignore
    RPCModule,
)

from hvm.constants import (
    TIME_BETWEEN_HEAD_HASH_SAVE,
    NUMBER_OF_HEAD_HASH_TO_SAVE,
    BLOCK_TIMESTAMP_FUTURE_ALLOWANCE, GAS_TX)

from hvm.utils.headers import (
    compute_gas_limit,
)
from hvm.chains.base import BaseChain

from helios.rlp_templates.hls import P2PBlock

import asyncio

from typing import (
    cast,
    Dict,
    Any,
    Union,
)
from helios.utils.validation import validate_transaction_call_dict
from hvm.utils.spoof import (
    SpoofTransaction,
)
from hvm.constants import ZERO_ADDRESS

from hp2p.events import NewBlockEvent, StakeFromBootnodeRequest, CurrentSyncStageRequest, \
    CurrentSyncingParametersRequest, GetConnectedNodesRequest, BlockImportQueueLengthRequest, \
    AverageNetworkMinGasPriceRequest, AverageNetworkMinGasPriceResponse

from eth_typing import Address

from hvm.rlp.consensus import StakeRewardBundle
from hvm.vm.forks.helios_testnet.blocks import HeliosMicroBlock

def account_db_at_block(chain, chain_address, at_block):
    if at_block == 'latest':
        header = chain.chaindb.get_canonical_head(chain_address=chain_address)
        account_hash = header.account_hash
        vm = chain.get_vm(header=header)
        return vm.state.account_db
    else:
        header = chain.chaindb.get_canonical_block_header_by_number(chain_address=chain_address, block_number=at_block)
        account_hash = header.account_hash
        vm = chain.get_vm(header=header)
        vm.state.account_db.revert_to_account_from_hash(account_hash, chain_address)
        return vm.state.account_db



def dict_to_spoof_transaction(
        chain,
        header,
        transaction_dict,
        ) -> SpoofTransaction:
    """
    Convert dicts used in calls & gas estimates into a spoof transaction
    """

    txn_dict = normalize_transaction_dict(transaction_dict)
    sender = txn_dict.get('from', ZERO_ADDRESS)

    if 'nonce' in txn_dict:
        nonce = txn_dict['nonce']
    else:
        vm = chain.get_vm(header)
        nonce = vm.state.account_db.get_nonce(sender)

    gas_price = txn_dict.get('gasPrice', 0)
    gas = txn_dict.get('gas', header.gas_limit)

    transaction = chain.create_transaction(
        nonce=nonce,
        gas_price=gas_price,
        gas=gas,
        to=txn_dict['to'],
        value=txn_dict['value'],
        data=txn_dict['data'],
        v=0,
        r=0,
        s=0
    )
    return SpoofTransaction(transaction, from_=sender)

def get_header(chain, at_block, chain_address):
    if at_block == 'latest':
        try:
            header = chain.chaindb.get_canonical_head(chain_address)
        except CanonicalHeadNotFound:
            header = None
    else:
        try:
            header = chain.chaindb.get_canonical_block_header_by_number(at_block, chain_address)
        except CanonicalHeadNotFound:
            try:
                header = chain.chaindb.get_canonical_head(chain_address)
            except CanonicalHeadNotFound:
                header = None
    return header

class Hls(RPCModule):
    '''
    All the methods defined by JSON-RPC API, starting with "hls_"...

    Any attribute without an underscore is publicly accessible.
    '''

    #
    # Tools
    #
    async def ping(self) -> bool:
        """
        Workaround for keepalive of ws connections in case it is not handled by the ws client.
        """
        return True

    async def chainId(self) -> str:
        return str(self._chain.network_id)

    async def accounts(self):
        raise DeprecationWarning("This method has been moved to personal_listAccounts")

    @format_params(identity, to_int_if_hex)
    async def call(self, txn_dict: Dict[str, Any], at_block: Union[str, int]) -> str:
        if 'from' not in txn_dict:
            raise BaseRPCError("hls_call requires from and to dict keys in the transaction dict.")
        chain_address = decode_hex(txn_dict['from'])
        chain = self.get_new_chain(chain_address)
        header = get_header(chain, at_block, chain_address)
        validate_transaction_call_dict(txn_dict, chain.get_vm(header))
        transaction = dict_to_spoof_transaction(chain, header, txn_dict)
        result = chain.get_transaction_result(transaction, header)
        return encode_hex(result)

    @format_params(decode_hex)
    async def blockNumber(self, chain_address):
        num = self._chain.get_canonical_head(chain_address).block_number
        return hex(num)




    @format_params(decode_hex, to_int_if_hex)
    async def getBalance(self, address, at_block):
        chain = self.get_new_chain(address)

        if at_block == 'latest':
            try:
                header = chain.chaindb.get_canonical_head(address)
                balance = header.account_balance
            except CanonicalHeadNotFound:
                balance = 0
        else:
            try:
                header = chain.chaindb.get_canonical_block_header_by_number(at_block, address)
                balance = header.account_balance
            except CanonicalHeadNotFound:
                try:
                    header = chain.chaindb.get_canonical_head(address)
                    balance = header.account_balance
                except CanonicalHeadNotFound:
                    balance = 0

        return hex(balance)


    @format_params(decode_hex)
    async def getBlockTransactionCountByHash(self, block_hash):
        chain = self.get_new_chain()
        try:
            tx_count = chain.chaindb.get_number_of_total_tx_in_block(block_hash)
        except HeaderNotFound:
            raise BaseRPCError('No block found with the given block hash')
        return hex(tx_count)

    @format_params(to_int_if_hex, decode_hex)
    async def getBlockTransactionCountByNumber(self, at_block, chain_address):
        chain = self.get_new_chain()
        try:
            block_hash = chain.chaindb.get_canonical_block_hash(chain_address=chain_address, block_number=at_block)
            tx_count = chain.chaindb.get_number_of_total_tx_in_block(block_hash)
        except HeaderNotFound:
            raise BaseRPCError('No block found with the given wallet address and block number')

        return hex(tx_count)

    @format_params(decode_hex, to_int_if_hex)
    async def getCode(self, chain_address, at_block):
        account_db = account_db_at_block(self._chain, chain_address, at_block)
        code = account_db.get_code(chain_address)
        return encode_hex(code)

    @format_params(decode_hex, to_int_if_hex, to_int_if_hex)
    async def getStorageAt(self, chain_address, position, at_block):
        if not is_integer(position) or position < 0:
            raise TypeError("Position of storage must be a whole number, but was: %r" % position)

        account_db = account_db_at_block(self._chain, chain_address, at_block)
        stored_val = account_db.get_storage(chain_address, position)
        return encode_hex(int_to_big_endian(stored_val))




    async def protocolVersion(self):
        return hex(63)

    async def syncing(self):
        # Check our current syncing stage. If not sync stage 4, then we are syncing
        current_sync_stage_response = await self._event_bus.request(
            CurrentSyncStageRequest()
        )
        if current_sync_stage_response.sync_stage < FULLY_SYNCED_STAGE_ID:
            return True
        else:
            return False



    #
    # Transactions
    #

    @format_params(decode_hex, to_int_if_hex)
    async def getTransactionByBlockHashAndIndex(self, block_hash, index):
        try:
            tx = self._chain.get_transaction_by_block_hash_and_index(block_hash, index)
        except HeaderNotFound:
            raise BaseRPCError('No block found with the given block hash')
        if isinstance(tx, BaseReceiveTransaction):
            # receive tx
            return receive_transaction_to_dict(tx, self._chain)
        else:
            # send tx
            return transaction_to_dict(tx, self._chain)

    @format_params(to_int_if_hex, to_int_if_hex, decode_hex)
    async def getTransactionByBlockNumberAndIndex(self, at_block, index, chain_address):
        try:
            block_hash = self._chain.chaindb.get_canonical_block_hash(chain_address=chain_address,
                                                                      block_number=at_block)
        except HeaderNotFound:
            raise BaseRPCError('No block found with the given chain address and block number')
        tx = self._chain.get_transaction_by_block_hash_and_index(block_hash, index)
        if isinstance(tx, BaseReceiveTransaction):
            # receive tx
            return receive_transaction_to_dict(tx, self._chain)
        else:
            # send tx
            return transaction_to_dict(tx, self._chain)

    @format_params(decode_hex, to_int_if_hex)
    async def getTransactionCount(self, chain_address, at_block):
        account_db = account_db_at_block(self._chain, chain_address, at_block)
        nonce = account_db.get_nonce(chain_address)
        return hex(nonce)

    @format_params(decode_hex)
    async def getTransactionByHash(self, tx_hash):
        chain = self.get_new_chain()
        try:
            tx = chain.get_canonical_transaction(tx_hash)
        except TransactionNotFound:
            raise BaseRPCError("Transaction with hash {} not found on canonical chain.".format(encode_hex(tx_hash)))
        if isinstance(tx, BaseReceiveTransaction):
            return receive_transaction_to_dict(tx, chain)
        else:
            return transaction_to_dict(tx, chain)

    @format_params(decode_hex)
    async def getTransactionReceipt(self, tx_hash):
        chain = self.get_new_chain()
        receipt = chain.chaindb.get_transaction_receipt(tx_hash)

        receipt_dict = receipt_to_dict(receipt, tx_hash, chain)

        return receipt_dict

    @format_params(decode_hex)
    async def getReceivableTransactions(self, chain_address):
        # create new chain for all requests
        chain = self.get_new_chain(chain_address)

        receivable_transactions = chain.create_receivable_transactions()
        receivable_transactions_dict = receive_transactions_to_dict(receivable_transactions, chain)

        return receivable_transactions_dict


    @format_params(identity, to_int_if_hex)
    async def filterAddressesWithReceivableTransactions(self, chain_addresses, after_timestamp = 0):
        #
        # Checks all of the given chain_addresses for receivable transactions, and returns a list of chain addresses that have any.
        #

        if len(chain_addresses) < 1:
            raise BaseRPCError("Must provide at least one chain address when calling getAddressesWithReceivableTransactions")

        earliest_chronological_timestamp = int(int(time.time())-TIME_BETWEEN_HEAD_HASH_SAVE*NUMBER_OF_HEAD_HASH_TO_SAVE*0.95)

        # create new chain for all requests
        chain = self.get_new_chain()
        chain_addresses = [Address(decode_hex(x)) for x in chain_addresses]

        if isinstance(after_timestamp, int) and after_timestamp > earliest_chronological_timestamp:
            # cycle through all chronological windows
            _, addresses_with_receivable_transactions = await chain.coro_get_receivable_transaction_hashes_from_chronological(after_timestamp, chain_addresses)
        else:
            addresses_with_receivable_transactions = await chain.coro_filter_accounts_with_receivable_transactions(chain_addresses)

        addresses_with_receivable_transactions = [to_checksum_address(x) for x in addresses_with_receivable_transactions]

        return addresses_with_receivable_transactions


    @format_params(decode_hex)
    async def getReceiveTransactionOfSendTransaction(self, tx_hash):
        '''
        Gets the receive transaction corresponding to a given send transaction, if it exists
        '''
        chain = self.get_new_chain()
        receive_tx = chain.get_receive_tx_from_send_tx(tx_hash)
        if receive_tx is not None:
            receive_tx_dict = receive_transaction_to_dict(receive_tx, chain)
            return receive_tx_dict
        else:
            raise BaseRPCError("No receive transaction found for the given send transaction hash")


    #
    # Gas system and network performance
    #
    async def gasPrice(self):
        # Make sure it meets the average min gas price of the network. If it doesnt, then it won't reach consensus.
        current_network_min_gas_price_response = await self._event_bus.request(
            AverageNetworkMinGasPriceRequest()
        )
        network_min_gas_price = current_network_min_gas_price_response.min_gas_price
        local_min_gas_price = self._chain.min_gas_db.get_required_block_min_gas_price()
        return hex(max([network_min_gas_price, local_min_gas_price]))

    async def getGasPrice(self):
        return await self.gasPrice()

    async def getHistoricalGasPrice(self):

        historical_min_gas_price = self._chain.min_gas_db.load_historical_minimum_gas_price()

        encoded = []
        for timestamp_gas_price in historical_min_gas_price:
            encoded.append([hex(timestamp_gas_price[0]), hex(timestamp_gas_price[1])])

        return encoded

    async def getApproximateHistoricalNetworkTPCCapability(self):

        historical_tpc_cap = self._chain.min_gas_db.load_historical_network_tpc_capability()

        encoded = []
        for timestamp_tpc_cap in historical_tpc_cap:
            encoded.append([hex(timestamp_tpc_cap[0]), hex(timestamp_tpc_cap[1])])

        return encoded

    async def getApproximateHistoricalTPC(self):

        historical_tpc = self._chain.chaindb.load_historical_tx_per_centisecond_from_chain()
        #historical_tpc = self._chain.min_gas_db.load_historical_tx_per_decisecond_from_imported()

        encoded = []
        for timestamp_tpc in historical_tpc:
            encoded.append([hex(timestamp_tpc[0]), hex(timestamp_tpc[1])])

        return encoded

    #
    # Blocks
    #
    @format_params(decode_hex, to_int_if_hex)
    async def getBlockNumber(self, chain_address, before_timestamp = None):
        chain = self.get_new_chain(chain_address)
        if before_timestamp is None or before_timestamp == 'latest':
            canonical_header = chain.chaindb.get_canonical_head(chain_address)
            block_number = canonical_header.block_number
        else:
            # it will raise HeaderNotFound error if there isnt one before the timestamp. This is on purpose.
            block_number = chain.chaindb.get_canonical_block_number_before_timestamp(before_timestamp, chain_address)
        return hex(block_number)


    @format_params(decode_hex)
    async def getBlockCreationParams(self, chain_address):
        #create new chain for all requests
        chain = self.get_new_chain(chain_address)

        to_return = {}

        to_return['block_number'] = hex(chain.header.block_number)
        to_return['parent_hash'] = encode_hex(chain.header.parent_hash)

        vm = chain.get_vm(timestamp = int(time.time()))

        to_return['nonce'] = hex(vm.state.account_db.get_nonce(chain_address))

        receivable_transactions = chain.create_receivable_transactions()
        encoded_receivable_transactions = []

        for re_tx in receivable_transactions:
            encoded_receivable_transactions.append(encode_hex(rlp.encode(re_tx)))

        to_return['receive_transactions'] = encoded_receivable_transactions

        reward_bundle = chain.get_consensus_db().create_reward_bundle_for_block(chain_address)
        amount = reward_bundle.reward_type_1.amount + reward_bundle.reward_type_2.amount
        to_return['reward_bundle'] = encode_hex(rlp.encode(reward_bundle, sedes = StakeRewardBundle))

        return to_return


    @format_params(decode_hex, identity)
    async def getBlockByHash(self, block_hash: Hash32, include_transactions: bool = False):
        chain = self.get_new_chain()
        block = chain.get_block_by_hash(block_hash)
        return block_to_dict(block, include_transactions, chain)


    @format_params(to_int_if_hex, decode_hex, identity)
    async def getBlockByNumber(self, at_block, chain_address, include_transactions: bool = False):
        chain = self.get_new_chain(chain_address)
        block = chain.get_block_by_number(at_block, chain_address=chain_address)
        return block_to_dict(block, include_transactions, chain)

    async def sendRawBlock(self, encoded_micro_block):

        chain = self.get_new_chain()

        encoded_micro_block = decode_hex(encoded_micro_block)

        # Assume this is using the vm for this timestamp. It should be if its coming in on the RPC
        micro_block = rlp.decode(encoded_micro_block, sedes=chain.get_vm(timestamp=Timestamp(int(time.time()))).micro_block_class)

        block_class = self._chain_class.get_vm_class_for_block_timestamp(timestamp = micro_block.header.timestamp).get_block_class()

        full_block = block_class.from_micro_block(micro_block)

        min_time_between_blocks = chain.get_vm(header=full_block.header).min_time_between_blocks

        # Validate the block here
        if(full_block.header.timestamp < (int(time.time()) - MAX_ALLOWED_AGE_OF_NEW_RPC_BLOCK)):
            raise BaseRPCError("The block timestamp is to old. We can only import new blocks over RPC.")

        if(full_block.header.timestamp > int(time.time() + BLOCK_TIMESTAMP_FUTURE_ALLOWANCE)):
            raise BaseRPCError("The block timestamp is in the future and cannot be accepted. You should check your computer clock.")

        try:
            canonical_head = chain.chaindb.get_canonical_head(full_block.header.chain_address)
            if canonical_head.block_number >= full_block.header.block_number:
                raise BaseRPCError("You are attempting to replace an existing block. This is not allowed.")

            if full_block.header.timestamp < (canonical_head.timestamp + min_time_between_blocks):
                raise BaseRPCError("Not enough time has passed for you to add a new block yet. New blocks can only be added to your chain every {} seconds".format(min_time_between_blocks))

        except CanonicalHeadNotFound:
            pass

        if((full_block.header.block_number != 0) and
            (not chain.chaindb.is_in_canonical_chain(full_block.header.parent_hash))):
            raise BaseRPCError("Parent block not found on canonical chain.")



        # Check our current syncing stage. Must be sync stage 4.
        current_sync_stage_response = await self._event_bus.request(
            CurrentSyncStageRequest()
        )
        if current_sync_stage_response.sync_stage < FULLY_SYNCED_STAGE_ID:
            raise BaseRPCError("This node is still syncing with the network. Please wait until this node has synced and try again.")


        # Check that our block import queue is not deep. It could cause min gas to increase before this block makes it there.
        current_block_import_queue_length = await self._event_bus.request(
            BlockImportQueueLengthRequest()
        )
        if current_block_import_queue_length.queue_length > MAX_ALLOWED_LENGTH_BLOCK_IMPORT_QUEUE:
            raise BaseRPCError("This node's block import queue is saturated. Please wait a moment and try again.")
        

        
        # Make sure it meets the local min gas price
        if not does_block_meet_min_gas_price(full_block, chain):
            required_min_gas_price = self._chain.min_gas_db.get_required_block_min_gas_price()
            raise Exception("Block transactions don't meet the minimum gas price requirement of {}".format(required_min_gas_price))

        # Make sure it meets the average min gas price of the network. If it doesnt, then it won't reach consensus.
        current_network_min_gas_price_response = await self._event_bus.request(
            AverageNetworkMinGasPriceRequest()
        )
        network_min_gas_price_wei = to_wei(current_network_min_gas_price_response.min_gas_price, 'gwei')
        if network_min_gas_price_wei > get_block_average_transaction_gas_price(full_block):
            raise BaseRPCError(
                "This block doesn't meet the minimum gas requirements to reach consensus on the network. It must have at least {} average gas price on all transactions.".format(current_network_min_gas_price_response.min_gas_price))

                
        self._event_bus.broadcast(
            NewBlockEvent(block=cast(P2PBlock, full_block), from_rpc=True)
        )

        return True

    #
    # Block explorer
    #
    @format_params(to_int_if_hex, to_int_if_hex, decode_hex_if_str, decode_hex_if_str, identity)
    async def getNewestBlocks(self, num_to_return = 10, start_idx = 0, after_hash = b'', chain_address = b'', include_transactions: bool = False):
        '''
        Returns list of block dicts
        :param start_idx:
        :param end_idx:
        :param chain_address:
        :return:
        '''
        # block = chain.get_block_by_hash(block_hash)
        # return block_to_dict(block, include_transactions, chain)
        if num_to_return is None:
            num_to_return = 10
        if start_idx is None:
            start_idx = 0
        num_to_return = min([10, num_to_return])
        block_dicts_to_return = []

        if chain_address != b'' and chain_address is not None:
            chain = self.get_new_chain(chain_address)
            try:
                canonical_header = chain.chaindb.get_canonical_head(chain_address)
                start = canonical_header.block_number-start_idx
                if start >= 0:
                    end = max([-1, start-num_to_return])
                    for i in range(start, end, -1):
                        block = chain.get_block_by_number(i, chain_address)
                        if block.hash == after_hash:
                            break
                        block_dicts_to_return.append(block_to_dict(block, include_transactions, chain))
                    
            except CanonicalHeadNotFound:
                return []
        else:
            chain = self.get_new_chain()
            at_block_index = -1
            current_window = int(time.time() / TIME_BETWEEN_HEAD_HASH_SAVE) * TIME_BETWEEN_HEAD_HASH_SAVE
            for timestamp in range(current_window, current_window-(NUMBER_OF_HEAD_HASH_TO_SAVE*TIME_BETWEEN_HEAD_HASH_SAVE), -1*TIME_BETWEEN_HEAD_HASH_SAVE):
                chronological_blocks = chain.chain_head_db.load_chronological_block_window(Timestamp(timestamp))
                if chronological_blocks is None:
                    continue
                chronological_blocks.reverse()

                for block_timestamp_block_hash in chronological_blocks:
                    at_block_index += 1
                    if at_block_index < start_idx:
                        continue

                    block = chain.get_block_by_hash(block_timestamp_block_hash[1])
                    if block.hash == after_hash:
                        return block_dicts_to_return

                    block_dicts_to_return.append(block_to_dict(block, include_transactions, chain))

                    if len(block_dicts_to_return) >= num_to_return:
                        return block_dicts_to_return

            
        return block_dicts_to_return

    #
    # Network status information
    #

    async def getConnectedNodes(self):

        get_connected_nodes_response = await self._event_bus.request(
            GetConnectedNodesRequest()
        )

        get_connected_nodes_response = get_connected_nodes_response.connected_nodes
        dict_to_output = connected_nodes_to_dict(get_connected_nodes_response)
        return dict_to_output

    #
    # Admin tools and dev debugging
    #
    async def getChronologicalBlockWindowTimestampHashes(self, timestamp: Timestamp):
        chain = self.get_new_chain()
        chronological_block_window = chain.chain_head_db.load_chronological_block_window(timestamp)

        return [[timestamp_root_hash[0], encode_hex(timestamp_root_hash[1])] for timestamp_root_hash in chronological_block_window]


    async def getHistoricalRootHashes(self):
        chain = self.get_new_chain()
        historical_root_hashes = chain.chain_head_db.get_historical_root_hashes()

        return [[timestamp_root_hash[0], encode_hex(timestamp_root_hash[1])] for timestamp_root_hash in historical_root_hashes]

    async def getCurrentSyncingParameters(self):
        current_syncing_parameters_request = await self._event_bus.request(
            CurrentSyncingParametersRequest()
        )

        sync_parameters = current_syncing_parameters_request.current_syncing_parameters
        if sync_parameters is None:
            return None
        else:
            return sync_parameters.__dict__


    # async def getBlockchainDBDetails(self):
    #     chain = self.get_new_chain()
    #     head_block_hashes = chain.chain_head_db.get_head_block_hashes()
    #
    #     return [encode_hex(head_block_hash) for head_block_hash in head_block_hashes]

    # async def getCurrentStakeFromBootnodeList(self):
    #     '''
    #     Returns the current list of node stakes that this node has already retrieved.
    #     For debugging purposes
    #     :return:
    #     '''
    #     stake_from_bootnode_response = await self._event_bus.request(
    #         StakeFromBootnodeRequest()
    #     )
    #     return [(encode_hex(address), stake) for address, stake in stake_from_bootnode_response.peer_stake_from_bootstrap_node.items()]

    # async def getAccountBalances(self):
    #     chain = self.get_new_chain()
    #     next_head_hashes = chain.chain_head_db.get_head_block_hashes_list()
    #
    #     wallet_addresses = []
    #     for next_head_hash in next_head_hashes:
    #         chain_address = chain.chaindb.get_chain_wallet_address_for_block_hash(next_head_hash)
    #         wallet_addresses.append(chain_address)
    #
    #     out = {}
    #     for wallet_address in wallet_addresses:
    #         out[encode_hex(wallet_address)] = chain.get_vm().state.account_db.get_balance(wallet_address)
    #
    #     return out

    # async def getBlockchainDatabase(self):
    #     chain_object = self.get_new_chain()
    #
    #     chain_head_hashes = chain_object.chain_head_db.get_head_block_hashes_list()
    #
    #     chains_dict = []
    #     for head_hash in chain_head_hashes:
    #         chain = chain_object.get_all_blocks_on_chain_by_head_block_hash(head_hash)
    #
    #         blocks_dict = []
    #         for block in chain:
    #             blocks_dict.append(block_to_dict(block, True, chain_object))
    #
    #         chains_dict.append(blocks_dict)
    #
    #     return chains_dict

    @format_params(decode_hex)
    async def getFaucet(self, chain_address):
        current_sync_stage_response = await self._event_bus.request(
            CurrentSyncStageRequest()
        )
        if current_sync_stage_response.sync_stage < FULLY_SYNCED_STAGE_ID:
            raise BaseRPCError("This node is still syncing with the network. Please wait until this node has synced.")

        try:
            from helios.helios_config import FAUCET_PRIVATE_KEY, ENABLE_FAUCET
        except ImportError:
            raise BaseRPCError("This node doesn't support the faucet.")

        if FAUCET_PRIVATE_KEY == '' or not ENABLE_FAUCET:
            raise BaseRPCError("This node doesn't support the faucet.")

        faucet_private_key = keys.PrivateKey(decode_hex(FAUCET_PRIVATE_KEY))
        chain_object = self.get_new_chain(faucet_private_key.public_key.to_canonical_address(), private_key= faucet_private_key)

        # make the chain read only for creating the block. We don't want to actually import it here.
        chain_object.enable_read_only_db()

        receivable_transactions, _ = chain_object.get_receivable_transactions(chain_address)
        total_receivable = 0
        for tx in receivable_transactions:
            total_receivable += tx.value

        faucet_balance = chain_object.get_vm().state.account_db.get_balance(faucet_private_key.public_key.to_canonical_address())
        if faucet_balance <= to_wei(2, 'ether'):
            raise BaseRPCError("The faucet is currently paused. Please try again later.")

        # Make sure it meets the average min gas price of the network. If it doesnt, then it won't reach consensus.
        current_network_min_gas_price_response = await self._event_bus.request(
            AverageNetworkMinGasPriceRequest()
        )

        network_min_gas_price = current_network_min_gas_price_response.min_gas_price
        local_min_gas_price = chain_object.min_gas_db.get_required_block_min_gas_price()
        actual_min_gas_price = max([network_min_gas_price, local_min_gas_price])

        if (chain_object.get_vm().state.account_db.get_balance(chain_address) + total_receivable) > to_wei(5, 'ether'):
            raise BaseRPCError("The selected account already has the maximum allowed HLS from the faucet.")

        chain_object.create_and_sign_transaction_for_queue_block(
            gas_price=to_wei(actual_min_gas_price+2, 'gwei'),
            gas=GAS_TX,
            to=chain_address,
            value=to_wei(1, 'ether'),
            data=b"",
            v=0,
            r=0,
            s=0
        )

        new_block = chain_object.import_current_queue_block()

        self._event_bus.broadcast(
            NewBlockEvent(block=cast(P2PBlock, new_block), from_rpc=True)
        )