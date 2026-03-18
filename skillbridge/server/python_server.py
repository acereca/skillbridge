from __future__ import annotations

import contextlib
import logging
from argparse import ArgumentParser
from collections.abc import Iterable
from logging import WARNING, basicConfig, getLogger
from os import getenv
from pathlib import Path
from select import select
from socketserver import (
    BaseRequestHandler,
    BaseServer,
    StreamRequestHandler,
    TCPServer,
    ThreadingMixIn,
    UnixStreamServer,
)
from sys import argv, platform, stderr, stdin, stdout
from sys import exit as sys_exit
from typing import TypeVar

LOG_DIRECTORY = Path(getenv('SKILLBRIDGE_LOG_DIRECTORY', '.'))
LOG_FILE = LOG_DIRECTORY / 'skillbridge_server.log'
LOG_FORMAT = '%(asctime)s %(levelname)s %(message)s'
LOG_DATE_FORMAT = '%d.%m.%Y %H:%M:%S'
LOG_LEVEL = WARNING

basicConfig(filename=LOG_FILE, format=LOG_FORMAT, datefmt=LOG_DATE_FORMAT)
logger = getLogger("python-server")


def send_to_skill(data: str) -> None:
    stdout.write(data)
    stdout.write("\n")
    stdout.flush()


def read_from_skill(timeout: float | None, force_tcp: bool) -> str:
    if platform == 'win32' or force_tcp:
        data_ready = data_tcp_ready
    else:
        data_ready = data_unix_ready

    readable = data_ready(timeout)

    if readable:
        return stdin.readline()

    logger.debug("timeout")
    return 'failure <timeout>'


class SingleTcpServer(TCPServer):
    skill_timeout: float = 0
    request_queue_size: int = 0
    allow_reuse_address: bool = True
    active: bool = False

    def __init__(self, port: str | int, handler: type[BaseRequestHandler]) -> None:
        super().__init__(('localhost', int(port)), handler)

    def server_bind(self) -> None:
        try:
            from socket import (  # type: ignore[attr-defined]  # noqa: PLC0415
                SIO_LOOPBACK_FAST_PATH,
            )

            self.socket.ioctl(  # type: ignore[attr-defined]
                SIO_LOOPBACK_FAST_PATH,
                True,  # noqa: FBT003
            )
        except ImportError:
            pass
        super().server_bind()


class ThreadingTcpServer(ThreadingMixIn, SingleTcpServer):
    pass


def create_tcp_server_class(single: bool) -> type[SingleTcpServer]:

    return SingleTcpServer if single else ThreadingTcpServer


def data_tcp_ready(timeout: float | None) -> bool:
    _ = timeout
    return True


class SingleUnixServer(UnixStreamServer):
    skill_timeout: float = 0
    request_queue_size: int = 0
    allow_reuse_address: bool = True

    def __init__(self, file: str, handler: type[BaseRequestHandler]) -> None:
        self.path = f'/tmp/skill-server-{file}.sock'
        with contextlib.suppress(FileNotFoundError):
            Path(self.path).unlink()

        super().__init__(self.path, handler)


class ThreadingUnixServer(ThreadingMixIn, SingleUnixServer):
    pass


def create_unix_server_class(single: bool) -> type[SingleUnixServer]:

    return SingleUnixServer if single else ThreadingUnixServer


def data_unix_ready(timeout: float | None) -> bool:
    readable, _, _ = select([stdin], [], [], timeout)

    return bool(readable)


ST = TypeVar("ST", bound=BaseServer)


class Handler(StreamRequestHandler):
    server: SingleTcpServer | SingleUnixServer

    def receive_all(self, remaining: int) -> Iterable[bytes]:
        while remaining:
            data = self.request.recv(remaining)
            remaining -= len(data)
            yield data

    def handle_one_request(self) -> bool:
        length = self.request.recv(10)
        if not length:
            logger.warning(f"client {self.client_address} lost connection")
            return False
        logger.debug(f"got length {length}")

        length = int(length)
        command = b''.join(self.receive_all(length))

        logger.debug(f"received {len(command)} bytes")

        if command.startswith(b'$close'):
            logger.debug(f"client {self.client_address} disconnected")
            return False
        logger.debug(f"got data {command[:1000].decode()}")

        send_to_skill(command.decode())
        logger.debug("sent data to skill")
        result = read_from_skill(
            self.server.skill_timeout,
            isinstance(self.server, TCPServer),
        ).encode()
        logger.debug(f"got response from skill {result[:1000]!r}")

        self.request.send(f'{len(result):10}'.encode())
        self.request.send(result)
        logger.debug("sent response to client")

        return True

    def try_handle_one_request(self) -> bool:
        try:
            return self.handle_one_request()
        except Exception:
            logger.exception("Failed to handle request")
            return False

    def handle(self) -> None:
        if self.reject:
            return

        logger.info(f"client {self.client_address} connected")
        client_is_connected = True
        while client_is_connected:
            client_is_connected = self.try_handle_one_request()

    def setup(self) -> None:
        if isinstance(self.server, SingleTcpServer) and self.server.active:
            self.request.close()
            self.reject = True
        elif isinstance(self.server, SingleTcpServer):
            self.server.active = True
            self.reject = False

    def finish(self) -> None:
        if not self.reject and isinstance(self.server, SingleTcpServer):
            self.server.active = False


def main(
    id_: str,
    log_level: str,
    notify: bool,
    single: bool,
    timeout: float | None,
    force_tcp: bool,
) -> None:
    logger.setLevel(getattr(logging, log_level))

    create_server_class = (
        create_tcp_server_class if (platform == 'win32') or force_tcp else create_unix_server_class
    )

    server_class = create_server_class(single)

    with server_class(id_, Handler) as server:
        server.skill_timeout = timeout or 0
        logger.info(
            f"starting server id={id_} log={log_level} {notify=} {single=} {timeout=} {force_tcp=}",
        )
        if notify:
            send_to_skill('running')
        server.serve_forever()


if __name__ == '__main__':
    log_levels = ["DEBUG", "WARNING", "INFO", "ERROR", "CRITICAL", "FATAL"]
    argument_parser = ArgumentParser(argv[0])
    argument_parser.add_argument('id')
    argument_parser.add_argument('log_level', choices=log_levels)
    argument_parser.add_argument('--notify', action='store_true')
    argument_parser.add_argument('--single', action='store_true')
    argument_parser.add_argument('--timeout', type=float, default=None)
    argument_parser.add_argument('--force-tcp', action='store_true')

    ns = argument_parser.parse_args()

    if platform == 'win32' and ns.timeout is not None:
        print("Timeout is not possible on Windows", file=stderr)
        sys_exit(1)

    with contextlib.suppress(KeyboardInterrupt):
        main(ns.id, ns.log_level, ns.notify, ns.single, ns.timeout, ns.force_tcp)
