#!/usr/bin/env python3
"""Exercise background assumeutxo yield, bounded header stall, stop, and normal sync."""
import time
from test_framework.authproxy import JSONRPCException
from test_framework.test_framework import BitcoinTestFramework
from test_framework.util import assert_equal
from test_framework.wallet import MiniWallet

START_HEIGHT = 199
BASE_HEIGHT = 299


class BackgroundYieldTest(BitcoinTestFramework):
    def set_test_params(self):
        self.num_nodes = 6
        self.rpc_timeout = 120
        self.extra_args = [["-debug=validation"] for _ in range(6)]

    def setup_network(self):
        self.add_nodes(6)
        self.start_nodes(extra_args=self.extra_args)

    def request_blocks(self, node, source, peer, first, last):
        for h in range(first, last + 1):
            block_hash = source.getblockhash(h)
            try:
                node.getblock(block_hash, 0)
                continue
            except JSONRPCException as exc:
                if not any(msg in exc.error["message"] for msg in ("Block not found", "Block not available")):
                    raise
            try:
                node.getblockfrompeer(block_hash, peer)
            except JSONRPCException as exc:
                if not any(msg in exc.error["message"] for msg in ("Block already downloaded", "Already requested")):
                    raise

    def run_test(self):
        source, stuck, normal, header_miner, stopping, restarting = self.nodes
        wallet = MiniWallet(source)
        # Identical 199->299 chain construction to feature_assumeutxo.py.
        for node in self.nodes:
            node.setmocktime(node.getblockheader(node.getbestblockhash())['time'])
        assert_equal(source.getblockcount(), START_HEIGHT)
        for i in range(100):
            if i % 3 == 0:
                wallet.send_self_transfer(from_node=source)
            self.generate(source, nblocks=1, sync_fun=self.no_op)
            if i == 4:
                previous = source.getbestblockhash()
                source.invalidateblock(previous)
                stale = self.generateblock(source, output="raw(aaaa)", transactions=[], sync_fun=self.no_op)["hash"]
                source.invalidateblock(stale)
                source.reconsiderblock(previous)
        assert_equal(source.getblockcount(), BASE_HEIGHT)
        snap = source.dumptxoutset('utxos.dat', "latest")
        for h in range(1, BASE_HEIGHT + 1):
            raw = source.getblock(source.getblockhash(h), 0)
            stuck.submitheader(raw)
            normal.submitheader(raw)
            stopping.submitheader(raw)
            restarting.submitheader(raw)
        assert_equal(stuck.loadtxoutset(snap['path'])['base_height'], BASE_HEIGHT)
        assert_equal(normal.loadtxoutset(snap['path'])['base_height'], BASE_HEIGHT)
        assert_equal(stopping.loadtxoutset(snap['path'])['base_height'], BASE_HEIGHT)
        assert_equal(restarting.loadtxoutset(snap['path'])['base_height'], BASE_HEIGHT)
        # As in feature_assumeutxo.py, the source must have a post-snapshot
        # continuation, otherwise it does not supply historical blocks.
        self.generate(source, nblocks=100, sync_fun=self.no_op)
        for h in range(BASE_HEIGHT + 1, BASE_HEIGHT + 101):
            raw = source.getblock(source.getblockhash(h), 0)
            stuck.submitheader(raw)
            normal.submitheader(raw)
            stopping.submitheader(raw)
            restarting.submitheader(raw)
        assert_equal(source.getblockcount(), BASE_HEIGHT + 100)
        # The header-miner receives all real blocks, then disconnects before
        # producing one header. The history source cannot serve its block.
        self.connect_nodes(0, 3)
        self.sync_blocks(nodes=(source, header_miner))
        self.disconnect_nodes(0, 3)
        self.generate(header_miner, nblocks=1, sync_fun=self.no_op)
        assert_equal(source.getblockcount(), BASE_HEIGHT + 100)
        stuck.submitheader(header_miner.getblock(header_miner.getbestblockhash(), 0))
        stopping.submitheader(header_miner.getblock(header_miner.getbestblockhash(), 0))
        assert_equal(stuck.getblockchaininfo()['headers'], BASE_HEIGHT + 101)
        self.log.info("Restart with incomplete background chainstate and remaining history on disk")
        self.connect_nodes(0, 5)
        restart_peer = restarting.getpeerinfo()[0]['id']
        self.request_blocks(restarting, source, restart_peer, START_HEIGHT + 1, BASE_HEIGHT)
        self.wait_until(
            lambda: START_HEIGHT < restarting.getchainstates()['chainstates'][0]['blocks'] < BASE_HEIGHT,
            timeout=60,
        )
        self.stop_node(5)
        self.start_node(5, extra_args=self.extra_args[5])
        restarting.getblockchaininfo()
        restart_log = restarting.debug_log_path.read_text(errors="replace")
        for unexpected in ("m_disabled", "Failed to connect best block"):
            assert unexpected not in restart_log
        self.wait_until(
            lambda: len(restarting.getchainstates()['chainstates']) == 1
            and restarting.getchainstates()['chainstates'][0]['validated'],
            timeout=240,
        )
        self.log.info("Yield and prompt stop with an unavailable newer block")
        with stopping.assert_debug_log(["[background validation] giving way to a newer block"]):
            self.connect_nodes(0, 4)
            peer = stopping.getpeerinfo()[0]['id']
            for h in range(START_HEIGHT + 1, BASE_HEIGHT + 1):
                stopping.getblockfrompeer(source.getblockhash(h), peer)
            self.wait_until(lambda: "giving way to a newer block" in stopping.debug_log_path.read_text(), timeout=30)
        started = time.monotonic()
        self.stop_node(4)
        stopped = time.monotonic() - started
        assert stopped < 15, f"background validation stop took {stopped:.2f}s"
        self.log.info("Separate snapshot node: unavailable header stalls then validates")
        with stuck.assert_debug_log(["[background validation] giving way to a newer block"]):
            self.connect_nodes(0, 1)
            peer = stuck.getpeerinfo()[0]['id']
            start = time.monotonic()
            for h in range(START_HEIGHT + 1, BASE_HEIGHT + 1):
                stuck.getblockfrompeer(source.getblockhash(h), peer)
            self.wait_until(lambda: len(stuck.getchainstates()['chainstates']) == 1, timeout=240)
        elapsed = time.monotonic() - start
        assert elapsed >= 55, f"background validation did not honor stall: {elapsed:.2f}s"
        assert_equal(stuck.getchainstates()['chainstates'][0]['validated'], True)
        self.log.info(f"Stall expired in {elapsed:.2f}s; stop took {stopped:.2f}s")
        self.log.info("Normal snapshot validation and a new active tip")
        self.connect_nodes(0, 2)
        self.wait_until(lambda: len(normal.getchainstates()['chainstates']) == 1, timeout=240)
        assert_equal(normal.getchainstates()['chainstates'][0]['validated'], True)
        self.generate(source, nblocks=1, sync_fun=self.no_op)
        self.sync_blocks(nodes=(source, normal))
        assert_equal(normal.getblockcount(), BASE_HEIGHT + 101)


if __name__ == '__main__':
    BackgroundYieldTest(__file__).main()
