from hvm.consensus import pow


class POWMiningMixin:
    """
    A VM that does POW mining as well. Should be used only in tests, when we
    need to programatically populate a ChainDB.
    """
    def finalize_block(self, block):
        block = super().finalize_block(block)  # type: ignore
        nonce, mix_hash = pow.mine_pow_nonce(
            block.number, block.header.mining_hash, block.header.difficulty)
        return block.copy(header=block.header.copy(nonce=nonce, mix_hash=mix_hash))
