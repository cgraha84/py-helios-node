from eth_typing import Hash32, Address
from eth_utils import encode_hex

from hvm.constants import BLOCK_GAS_LIMIT
from hvm.exceptions import ValidationError
from hvm.utils.address import generate_contract_address
from hvm.utils.rlp import diff_rlp_object
from hvm.utils.spoof import SpoofTransaction
from hvm.vm.forks.photon.consensus import PhotonConsensusDB
from hvm.vm.forks.photon.utils import ensure_computation_call_send_transactions_are_equal
from hvm.vm.message import Message

from .constants import (
    EIP658_TRANSACTION_STATUS_CODE_FAILURE,
    EIP658_TRANSACTION_STATUS_CODE_SUCCESS,
)

from .validation import validate_photon_transaction_against_header

from .blocks import (
    PhotonBlock, PhotonQueueBlock, PhotonMicroBlock)

from .headers import (create_photon_header_from_parent, configure_photon_header)
from .state import PhotonState
from hvm.vm.base import VM

from hvm.rlp.receipts import (
    Receipt,
)

from .transactions import (
    PhotonTransaction,
    PhotonReceiveTransaction,
)

from .computation import PhotonComputation

from hvm.rlp.headers import BaseBlockHeader, BlockHeader

from hvm.vm.forks.boson import make_boson_receipt

from typing import Tuple, List

from eth_bloom import (
    BloomFilter,
)

import functools

from eth_keys.datatypes import PrivateKey

def make_photon_receipt(base_header: BaseBlockHeader,
                                computation: PhotonComputation,
                                send_transaction: PhotonTransaction,
                                receive_transaction: PhotonReceiveTransaction = None,
                                refund_transaction: PhotonReceiveTransaction = None,
                                ) -> Receipt:

    return make_boson_receipt(base_header,
                                       computation,
                                       send_transaction,
                                       receive_transaction,
                                       refund_transaction)


class PhotonVM(VM):
    # fork name
    fork = 'photon'

    # classes
    micro_block_class = PhotonMicroBlock
    block_class = PhotonBlock
    queue_block_class = PhotonQueueBlock
    _state_class = PhotonState

    # Methods
    create_header_from_parent = staticmethod(create_photon_header_from_parent)
    configure_header = configure_photon_header
    make_receipt = staticmethod(make_photon_receipt)
    validate_transaction_against_header = validate_photon_transaction_against_header
    consensus_db_class = PhotonConsensusDB

    min_time_between_blocks = constants.MIN_TIME_BETWEEN_BLOCKS

    def generate_transaction_for_single_computation(self,
                                                    tx_data: bytes,
                                                    from_address: Address,
                                                    to_address: Address,
                                                    **kwargs,
                                                    ) -> SpoofTransaction:
        tx_nonce = self.state.account_db.get_nonce(from_address)
        if from_address == self.header.chain_address:
            # This chain is the from address, so it should be execute on send
            execute_on_send = True
        else:
            execute_on_send = False

        transaction = self.create_transaction(
            gas_price=0x00,
            gas=BLOCK_GAS_LIMIT,
            to=to_address,
            value=0,
            nonce=tx_nonce,
            data=tx_data,
            execute_on_send = execute_on_send,
            **kwargs,
        )

        return SpoofTransaction(transaction, from_=from_address)



    def apply_all_transactions(self, block: PhotonBlock, private_key: PrivateKey = None) -> Tuple[
                                                                                        BaseBlockHeader,
                                                                                        List[Receipt],
                                                                                        List[PhotonComputation],
                                                                                        List[PhotonComputation],
                                                                                        List[PhotonReceiveTransaction],
                                                                                        List[PhotonTransaction]]:

        # First, run all of the receive transactions
        last_header, receive_receipts, receive_computations, processed_receive_transactions = self._apply_all_receive_transactions(block.receive_transactions, block.header)

        current_nonce_for_computation_calls = None

        computation_call_send_transactions = []
        for receive_computation in receive_computations:
            if receive_computation.msg.data != b'' and not receive_computation.is_error:

                # Only check if there is actually transaction data because this will be an expensive function
                external_call_messages = receive_computation.get_all_children_external_call_messages()

                if len(external_call_messages) > 0:
                    gas_price = receive_computation.transaction_context.gas_price

                    # Do this in here for performance. We only compute it if there are computation calls.
                    if current_nonce_for_computation_calls is None:
                        current_nonce_for_computation_calls = self.get_nonce_for_computation_calls(block)

                    if receive_computation.transaction_context.is_computation_call_origin:
                        origin = receive_computation.transaction_context.tx_origin
                    else:
                        # Needs to be the code address that generated this
                        origin = receive_computation.transaction_context.smart_contract_storage_address


                    for i in range(len(external_call_messages)):
                        call_message = external_call_messages[i]
                        gas = call_message.gas
                        code_address = call_message.code_address if call_message.code_address is not None else b''

                        execute_on_send = call_message.execute_on_send

                        #todo: need to allow for create2 addresses here, in which the salt must be used. how do we tell
                        # the transaction to use this kind instead of the nonce kind. Precompile?
                        if call_message.is_create:
                            self.validate_create_call(call_message,
                                                     block.header.chain_address,
                                                     current_nonce_for_computation_calls
                                                     )

                        new_tx = self.create_transaction(
                            nonce = current_nonce_for_computation_calls,
                            gas_price=gas_price,
                            gas=gas,
                            to=call_message.to,
                            value=call_message.value,
                            data=call_message.data,
                            caller = block.header.chain_address,
                            origin = origin,
                            code_address = code_address,
                            execute_on_send = execute_on_send
                        )

                        self.logger.debug("Creating a new child transaction with parameters:"
                                          "nonce: {} | gas_price: {} | gas: {} | to: {} | "
                                          "value: {} | data: {} | "
                                          "caller: {} | origin: {} | "
                                          "code_address: {} | execute_on_send: {}".format(
                            new_tx.nonce, new_tx.gas_price, new_tx.gas, encode_hex(new_tx.to),
                            new_tx.value, encode_hex(new_tx.data), encode_hex(new_tx.caller),
                            encode_hex(new_tx.origin), encode_hex(new_tx.code_address), new_tx.execute_on_send
                        ))

                        new_tx = new_tx.get_signed(private_key, self.network_id)

                        computation_call_send_transactions.append(new_tx)

                        current_nonce_for_computation_calls += 1



        # TODO: then create the new transactions and add them to the block. But only add them if they don't already exist there.
        # Only add them to the block if it is a queueblock. Otherwise, just check to make sure all tx params are identical except
        # for the signature.
        # Need a check - send transactions can only originate from a computation. If there are more send transactions than
        # came out of these computations - it is an invalid block.
        #
        # When processing send transactions on a smart contract, subtract value like normal. But we have to make sure that the
        # transaction originated from code on this chain. NO - we dont process normally, because the signing sender wont be the one paying
        # It needs to subtract any value from this smart contract account instead.
        #
        # We also need to make sure the VM doesnt subtract any gas for these transactions. The gas has already been subtracted.
        #
        # Who is going to sign these transactions? The sender needs to be the person who sent the first transaction so that they
        # can be correctly refunded. But they arent here to sign it... Add another field to the transaction for refund address?


        if len(computation_call_send_transactions) > 0:
            normal_send_transactions, _ = self.separate_normal_transactions_and_computation_calls(block.transactions)
            normal_send_transactions.extend(computation_call_send_transactions)
            send_transactions = normal_send_transactions
        else:
            send_transactions = block.transactions

        # Then, run all of the send transactions
        last_header, receipts, send_computations = self._apply_all_send_transactions(send_transactions, last_header)

        # Combine receipts in the send transaction, receive transaction order
        receipts.extend(receive_receipts)

        return last_header, receipts, receive_computations, send_computations, processed_receive_transactions, computation_call_send_transactions


    def save_recievable_transactions(self,block_header_hash: Hash32, computations: List[PhotonComputation], receive_transactions: List[PhotonReceiveTransaction]) -> None:
        for computation in computations:
            msg = computation.msg
            transaction_context = computation.transaction_context
            self.state.account_db.add_receivable_transaction(msg.resolved_to,
                                                             transaction_context.send_tx_hash,
                                                             block_header_hash,
                                                             msg.is_create)

        # Refunds
        for receive_transaction in receive_transactions:
            if not receive_transaction.is_refund and receive_transaction.remaining_refund != 0:
                send_transaction = self.chaindb.get_transaction_by_hash(receive_transaction.send_transaction_hash,
                                                                        send_tx_class = self.get_block_class().transaction_class,
                                                                        receive_tx_class=self.get_block_class().receive_transaction_class)
                refund_address = send_transaction.refund_address
                self.logger.debug("SAVING RECEIVABLE REFUND TX WITH HASH {} ON CHAIN {}: {}".format(encode_hex(receive_transaction.hash), encode_hex(refund_address), receive_transaction.as_dict()))

                self.state.account_db.add_receivable_transaction(refund_address,
                                                                 receive_transaction.hash,
                                                                 block_header_hash)

    def apply_receipt_to_header(self, base_header: BaseBlockHeader, receipt: Receipt) -> BaseBlockHeader:
        new_header = base_header.copy(
            bloom=int(BloomFilter(base_header.bloom) | receipt.bloom),
            gas_used=base_header.gas_used + receipt.gas_used,
        )
        return new_header


    def contains_computation_calls(self, send_transactions: List[PhotonTransaction]) -> bool:
        # Caution: this function assumes computation calls are at the end of the list if they exist
        if len(send_transactions) == 0:
            return False
        else:
            return send_transactions[-1].created_by_computation


    def separate_normal_transactions_and_computation_calls(self, send_transactions: List[PhotonTransaction]) -> Tuple[List[PhotonTransaction], List[PhotonTransaction]]:
        normal_transactions = []
        computation_transactions = []
        computation_call_found = False
        for tx in send_transactions:
            if tx.created_by_computation:
                computation_transactions.append(tx)
                computation_call_found = True
            else:
                if computation_call_found:
                    raise ValidationError("Normal send transaction came after a computation call send transaction. This is not allowed.")
                normal_transactions.append(tx)

        return normal_transactions, computation_transactions

    def get_next_nonce_after_normal_transactions(self, send_transactions: List[PhotonTransaction]) -> int:
        if len(send_transactions) == 0:
            raise ValueError("Cannot get next nonce after normal transactions because the transaction list is empty.")

        if not self.contains_computation_calls(send_transactions):
            return send_transactions[-1].nonce + 1
        else:
            normal_transactions, computation_calls = self.separate_normal_transactions_and_computation_calls(send_transactions)
            if len(normal_transactions) > 0:
                return normal_transactions[-1].nonce + 1
            else:
                return computation_calls[0].nonce

    def get_nonce_for_computation_calls(self, block: PhotonBlock) -> int:
        if len(block.transactions) == 0:
            nonce = self.state.account_db.get_nonce(block.header.chain_address)
        else:
            nonce = self.get_next_nonce_after_normal_transactions(block.transactions)
        return nonce

    def add_computation_call_nonce_to_execution_context(self, block):
        nonce = self.get_nonce_for_computation_calls(block)
        self.state.execution_context.computation_call_nonce = nonce


    #
    # Validation
    #
    
    def validate_computation_call_send_transactions_against_block(self, block: PhotonBlock, computation_call_send_transactions: List[PhotonTransaction]) -> None:
        '''
        This function ensures that the computation call send transactions in the given block are the same as the ones the local
        VM produced. All parameters of the transactions should be the same except for the signature.
        :param block:
        :param computation_call_send_transactions:
        :return:
        '''
        self.logger.debug("Validating computation call send transactions in block vs the ones our VM generated.")
        send_transactions = block.transactions
        # This function also ensures that the transactions are in the correct order with computations after normal
        _, block_computation_call_send_transactions = self.separate_normal_transactions_and_computation_calls(send_transactions)

        if len(block_computation_call_send_transactions) != len(computation_call_send_transactions):
            raise ValidationError("The number of computation call send transactions in the block differ from the number of ones the local VM generated."
                                  "Number in block: {}, number generated here: {}".format(len(block_computation_call_send_transactions), len(computation_call_send_transactions)))

        for i in range(len(block_computation_call_send_transactions)):
            ensure_computation_call_send_transactions_are_equal(block_computation_call_send_transactions[i], computation_call_send_transactions[i])

    def validate_create_call(self,
                             call_message: Message,
                             this_chain_address: Address,
                             current_nonce_for_computation_calls: int
                             ) -> None:
        if call_message.nonce != current_nonce_for_computation_calls:
            raise ValidationError(
                "A computation call or create was generated with a nonce that is different from what it should be. "
                "The nonce used to generate the call: {} | what it should be: {}".format(
                    call_message.nonce, current_nonce_for_computation_calls
                ))

        # double check that the contract address is the correct one for this nonce
        contract_address = generate_contract_address(
            this_chain_address,
            current_nonce_for_computation_calls,
        )
        if contract_address != call_message.create_address:
            raise ValidationError(
                "A create message generated the incorrect contract address for this nonce."
                "nonce: {} | generated contract address: {} | expected contract address: {}".format(
                    current_nonce_for_computation_calls,
                    encode_hex(call_message.create_address),
                    encode_hex(contract_address)
                ))
