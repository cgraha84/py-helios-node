from eth_typing import (
    Address
)

#
# Gas Costs and Refunds
#
REFUND_SELFDESTRUCT = 24000
GAS_CODEDEPOSIT = 200


EIP658_TRANSACTION_STATUS_CODE_FAILURE = b''
EIP658_TRANSACTION_STATUS_CODE_SUCCESS = b'\x01'

#
# Gas Costs and Refunds
#
GAS_CODEDEPOSIT = 200


# https://github.com/ethereum/EIPs/issues/160
GAS_EXP_EIP160 = 10
GAS_EXPBYTE_EIP160 = 50


# https://github.com/ethereum/EIPs/issues/170
EIP170_CODE_SIZE_LIMIT = 24577

#
# Gas Costs and Refunds
#
GAS_SELFDESTRUCT_EIP150 = 5000
GAS_CALL_EIP150 = 700
GAS_EXTCODE_EIP150 = 700
GAS_BALANCE_EIP150 = 400
GAS_SLOAD_EIP150 = 200