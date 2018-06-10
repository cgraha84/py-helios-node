import asyncio
import logging
import signal
import sys
import os
from typing import Type

from evm.chains.mainnet import (
    MAINNET_NETWORK_ID,
)

from evm.db.backends.base import BaseDB
from evm.db.backends.level import LevelDB

from p2p.service import BaseService

from trinity.exceptions import (
    AmbigiousFileSystem,
    MissingPath,
)
from trinity.chains import (
    initialize_data_dir,
    is_data_dir_initialized,
    serve_chaindb,
)
from trinity.console import (
    console,
)
from trinity.cli_parser import (
    parser,
)
from trinity.config import (
    ChainConfig,
)
from trinity.utils.ipc import (
    wait_for_ipc,
    kill_process_gracefully,
)
from trinity.utils.logging import (
    setup_trinity_stdout_logging,
    setup_trinity_file_and_queue_logging,
    with_queued_logging,
)
from trinity.utils.mp import (
    ctx,
)
from trinity.utils.profiling import (
    setup_cprofiler,
)
from trinity.utils.version import (
    construct_trinity_client_identifier,
)


PRECONFIGURED_NETWORKS = {MAINNET_NETWORK_ID}

HELIOS_HEADER = (
    "\n"    
    " __  __     ______     __         __     ______     ______    \n"
    "/\ \_\ \   /\  ___\   /\ \       /\ \   /\  __ \   /\  ___\   \n"
    "\ \  __ \  \ \  __\   \ \ \____  \ \ \  \ \ \/\ \  \ \___  \  \n"
    " \ \_\ \_\  \ \_____\  \ \_____\  \ \_\  \ \_____\  \/\_____\ \n"
    "  \/_/\/_/   \/_____/   \/_____/   \/_/   \/_____/   \/_____/ \n"
)   

TRINITY_AMBIGIOUS_FILESYSTEM_INFO = (
    "Could not initialize data directory\n\n"
    "   One of these conditions must be met:\n"
    "   * HOME environment variable set\n"
    "   * XDG_TRINITY_ROOT environment variable set\n"
    "   * TRINITY_DATA_DIR environment variable set\n"
    "   * --data-dir command line argument is passed\n"
    "\n"
    "   In case the data directory is outside of the trinity root directory\n"
    "   Make sure all paths are pre-initialized as Trinity won't attempt\n"
    "   to create directories outside of the trinity root directory\n"
)


def main(instance_number = None) -> None:
    args = parser.parse_args()

    log_level = getattr(logging, args.log_level.upper())

    if args.network_id not in PRECONFIGURED_NETWORKS:
        raise NotImplementedError(
            "Unsupported network id: {0}.  Only the ropsten and mainnet "
            "networks are supported.".format(args.network_id)
        )

    logger, formatter, handler_stream = setup_trinity_stdout_logging(log_level)

    
    if args.instance is not None:
        os.environ["XDG_TRINITY_SUBDIRECTORY"] = 'instance_'+str(args.instance)
    elif instance_number is not None:
        os.environ["XDG_TRINITY_SUBDIRECTORY"] = 'instance_'+str(instance_number)
        
    #args.data_dir = '/d:/Google Drive/forex/blockchain coding/Helios/prototype desktop/py-evm/trinity/data/'
    try:
        chain_config = ChainConfig.from_parser_args(args)
    except AmbigiousFileSystem:
        exit_because_ambigious_filesystem(logger)
        

    if not is_data_dir_initialized(chain_config):
        # TODO: this will only work as is for chains with known genesis
        # parameters.  Need to flesh out how genesis parameters for custom
        # chains are defined and passed around.
        try:
            initialize_data_dir(chain_config)
        except AmbigiousFileSystem:
            exit_because_ambigious_filesystem(logger)
        except MissingPath as e:
            msg = (
                "\n"
                "It appears that {} does not exist.\n"
                "Trinity does not attempt to create directories outside of its root path\n"
                "Either manually create the path or ensure you are using a data directory\n"
                "inside the XDG_TRINITY_ROOT path"
            ).format(e.path)
            logger.error(msg)
            sys.exit(1)

    logger, log_queue, listener = setup_trinity_file_and_queue_logging(
        logger,
        formatter,
        handler_stream,
        chain_config,
        log_level
    )
    
    # if console command, run the trinity CLI
    if args.subcommand == 'attach':
        console(chain_config.jsonrpc_ipc_path, use_ipython=not args.vanilla_shell)
        sys.exit(0)

    # start the listener thread to handle logs produced by other processes in
    # the local logger.
    listener.start()

    extra_kwargs = {
        'log_queue': log_queue,
        'log_level': log_level,
        'profile': args.profile,
    }

    #First initialize the database process.
    database_server_process = ctx.Process(
        target=run_database_process,
        args=(
            chain_config,
            LevelDB,
        ),
        kwargs=extra_kwargs,
    )

    networking_process = ctx.Process(
        target=launch_node,
        args=(chain_config, ),
        kwargs=extra_kwargs,
    )

    #start the processes
    database_server_process.start()
    logger.info("Started DB server process (pid=%d)", database_server_process.pid)
    wait_for_ipc(chain_config.database_ipc_path)

    networking_process.start()
    logger.info("Started networking process (pid=%d)", networking_process.pid)
    
    try:
        if args.subcommand == 'console':
            console(chain_config.jsonrpc_ipc_path, use_ipython=not args.vanilla_shell)
        else:
            networking_process.join()
    except KeyboardInterrupt:
        # When a user hits Ctrl+C in the terminal, the SIGINT is sent to all processes in the
        # foreground *process group*, so both our networking and database processes will terminate
        # at the same time and not sequentially as we'd like. That shouldn't be a problem but if
        # we keep getting unhandled BrokenPipeErrors/ConnectionResetErrors like reported in
        # https://github.com/ethereum/py-evm/issues/827, we might want to change the networking
        # process' signal handler to wait until the DB process has terminated before doing its
        # thing.
        # Notice that we still need the kill_process_gracefully() calls here, for when the user
        # simply uses 'kill' to send a signal to the main process, but also because they will
        # perform a non-gracefull shutdown if the process takes too long to terminate.
        logger.info('Keyboard Interrupt: Stopping')
        kill_process_gracefully(database_server_process, logger)
        logger.info('DB server process (pid=%d) terminated', database_server_process.pid)
        kill_process_gracefully(networking_process, logger)
        logger.info('Networking process (pid=%d) terminated', networking_process.pid)


@setup_cprofiler('run_database_process')
@with_queued_logging
def run_database_process(chain_config: ChainConfig, db_class: Type[BaseDB]) -> None:
    base_db = db_class(db_path=chain_config.database_dir)
    serve_chaindb(chain_config, base_db)


def exit_because_ambigious_filesystem(logger: logging.Logger) -> None:
    logger.error(TRINITY_AMBIGIOUS_FILESYSTEM_INFO)
    sys.exit(1)


async def exit_on_signal(service_to_exit: BaseService) -> None:
    loop = asyncio.get_event_loop()
    sigint_received = asyncio.Event()
    for sig in [signal.SIGINT, signal.SIGTERM]:
        # TODO also support Windows
        loop.add_signal_handler(sig, sigint_received.set)

    await sigint_received.wait()
    try:
        await service_to_exit.cancel()
    finally:
        loop.stop()


@setup_cprofiler('launch_node')
@with_queued_logging
def launch_node(chain_config: ChainConfig) -> None:
    display_launch_logs(chain_config)
    
    NodeClass = chain_config.node_class
    node = NodeClass(chain_config)
    
    run_service_until_quit(node)


def display_launch_logs(chain_config: ChainConfig) -> None:
    logger = logging.getLogger('trinity')
    logger.info(HELIOS_HEADER)
    logger.info(construct_trinity_client_identifier())


def run_service_until_quit(service: BaseService) -> None:
    loop = asyncio.get_event_loop()
    asyncio.ensure_future(exit_on_signal(service))
    asyncio.ensure_future(service.run())
    loop.run_forever()
    loop.close()


if __name__ == "__main__":
    __spec__ = 'Nones'
    main(1)
