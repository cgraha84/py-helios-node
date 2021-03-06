import pytest

from helios.tools.async_process_runner import AsyncProcessRunner
from helios.utils.async_iter import (
    contains_all
)


# IMPORTANT: Test names are intentionally short here because they end up
# in the path name of the isolated Trinity paths that pytest produces for
# us.
# e.g. /tmp/pytest-of-circleci/pytest-0/popen-gw3/test_light_boot_comman0/xdg/mainnet/jsonrpc.ipc)
#
# However, UNIX IPC paths can only be 100 chars which means long paths
# *WILL* break these tests. See: https://unix.stackexchange.com/q/367008

# This fixture provides a tear down to run after each test that uses it.
# This ensures the AsyncProcessRunner will never leave a process behind
@pytest.fixture(scope="function")
def async_process_runner(event_loop):
    runner = AsyncProcessRunner(
        # This allows running pytest with -s and observing the output
        debug_fn=lambda line: print(line)
    )
    yield runner
    try:
        runner.kill()
    except ProcessLookupError:
        pass

# Great for debugging the AsyncProcessRunner
# @pytest.mark.asyncio
# async def test_ping(async_process_runner):
#     await async_process_runner.run(['ping', 'www.google.de'])
#     assert await contains_all(async_process_runner.iterate_stdout(), ['byytes from'])


@pytest.mark.parametrize(
    'command',
    (
        ('helios',),
        ('helios', '--ropsten',),
    )
)
@pytest.mark.asyncio
async def test_full_boot(async_process_runner, command):
    # UPNP discovery can delay things, we use a timeout longer than the discovery timeout
    await async_process_runner.run(command, timeout_sec=40)
    assert await contains_all(async_process_runner.stderr, {
        "Started DB server process",
        "Started networking process",
        "Running server",
        "IPC started at",
    })


@pytest.mark.parametrize(
    'command',
    (
        ('helios', '--tx-pool',),
        ('helios', '--tx-pool', '--ropsten',),
    )
)
@pytest.mark.asyncio
async def test_txpool_full_boot(async_process_runner, command):
    # UPNP discovery can delay things, we use a timeout longer than the discovery timeout
    await async_process_runner.run(command, timeout_sec=40)
    assert await contains_all(async_process_runner.stderr, {
        "Started DB server process",
        "Started networking process",
        "Running Tx Pool",
        "Running server",
        "IPC started at",
    })


@pytest.mark.parametrize(
    'command',
    (
        ('helios', '--light', '--tx-pool',),
        ('helios', '--light', '--ropsten', '--tx-pool',),
    )
)
@pytest.mark.asyncio
async def test_txpool_deactivated(async_process_runner, command):
    await async_process_runner.run(command)
    assert await contains_all(async_process_runner.stderr, {
        "Started DB server process",
        "Started networking process",
        "The transaction pool is not yet available in light mode",
    })


@pytest.mark.parametrize(
    'command',
    (
        ('helios', '--light',),
        ('helios', '--light', '--ropsten',),
    )
)
@pytest.mark.asyncio
async def test_light_boot(async_process_runner, command):
    # UPNP discovery can delay things, we use a timeout longer than the discovery timeout
    await async_process_runner.run(command, timeout_sec=40)
    assert await contains_all(async_process_runner.stderr, {
        "Started DB server process",
        "Started networking process",
        "IPC started at",
    })


@pytest.mark.parametrize(
    'command',
    (
        # mainnet
        ('helios',),
        ('helios', '--tx-pool',),
        ('helios', '--light',),
        # ropsten
        ('helios', '--ropsten',),
        ('helios', '--ropsten', '--tx-pool',),
        ('helios', '--light', '--ropsten',),
    )
)
@pytest.mark.asyncio
async def test_does_not_throw(async_process_runner, command):
    # This is our last line of defence. This test basically observes the first
    # 20 seconds of the Trinity boot process and fails if Trinity logs any exceptions
    lines_since_error = 0
    await async_process_runner.run(command, timeout_sec=20)
    async for line in async_process_runner.stderr:

        # We detect errors by some string at the beginning of the Traceback and keep
        # counting lines from there to be able to read and report more valuable info
        if "Traceback (most recent call last)" in line and lines_since_error == 0:
            lines_since_error = 1
        elif lines_since_error > 0:
            lines_since_error += 1

        # Keep on listening for output for a maxmimum of 100 lines after the error
        if lines_since_error >= 100:
            break

    if lines_since_error > 0:
        raise Exception("Exception during Trinity boot detected")
