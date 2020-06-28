import math
import socket
import sys
from concurrent.futures._base import Future
from typing import Callable, Optional, List, Union, ClassVar, Generic, TypeVar, Type, Awaitable

import attr
import trio.from_thread
import trio.lowlevel
from outcome import Value, Error
from trio.to_thread import run_sync

from .. import claim_worker_thread, TaskInfo, MessageStream
from ..abc.networking import (
    TCPSocketStream as AbstractTCPSocketStream, UNIXSocketStream as AbstractUNIXSocketStream,
    UDPPacket, UDPSocket as AbstractUDPSocket, ConnectedUDPSocket as AbstractConnectedUDPSocket,
    TCPListener as AbstractTCPListener, UNIXListener as AbstractUNIXListener
)
from ..abc.streams import ByteStream, ReceiveByteStream, SendByteStream
from ..abc.subprocesses import AsyncProcess as AbstractAsyncProcess
from ..abc.synchronization import (
    Lock as AbstractLock, Condition as AbstractCondition, Event as AbstractEvent,
    Semaphore as AbstractSemaphore, CapacityLimiter as AbstractCapacityLimiter)
from ..abc.tasks import (
    CancelScope as AbstractCancelScope, TaskGroup as AbstractTaskGroup,
    BlockingPortal as AbstractBlockingPortal)
from ..abc.testing import TestRunner as AbstractTestRunner
from ..exceptions import (
    ExceptionGroup as BaseExceptionGroup, WouldBlock, ClosedResourceError, BusyResourceError)

if sys.version_info < (3, 7):
    from async_generator import asynccontextmanager
else:
    from contextlib import asynccontextmanager

T_Retval = TypeVar('T_Retval')
T_Item = TypeVar('T_Item')

#
# Event loop
#

run = trio.run


#
# Miscellaneous
#

class finalize:
    def __init__(self, aiter):
        self._aiter = aiter

    async def __aenter__(self):
        return self._aiter

    async def __aexit__(self, *args):
        await self._aiter.aclose()


sleep = trio.sleep


#
# Timeouts and cancellation
#

CancelledError = trio.Cancelled


class CancelScope:
    __slots__ = '__original'

    def __init__(self, original: Optional[trio.CancelScope] = None, **kwargs):
        self.__original = original or trio.CancelScope(**kwargs)

    async def __aenter__(self):
        if self.__original._has_been_entered:
            raise RuntimeError(
                "Each CancelScope may only be used for a single 'async with' block"
            )

        self.__original.__enter__()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        return self.__original.__exit__(exc_type, exc_val, exc_tb)

    async def cancel(self) -> None:
        self.__original.cancel()

    @property
    def deadline(self) -> float:
        return self.__original.deadline

    @property
    def cancel_called(self) -> bool:
        return self.__original.cancel_called

    @property
    def shield(self) -> bool:
        return self.__original.shield


AbstractCancelScope.register(CancelScope)


@asynccontextmanager
async def move_on_after(seconds, shield):
    with trio.move_on_after(seconds) as scope:
        scope.shield = shield
        yield CancelScope(scope)


@asynccontextmanager
async def fail_after(seconds, shield):
    try:
        with trio.fail_after(seconds) as cancel_scope:
            cancel_scope.shield = shield
            yield CancelScope(cancel_scope)
    except trio.TooSlowError as exc:
        raise TimeoutError().with_traceback(exc.__traceback__) from None


async def current_effective_deadline():
    return trio.current_effective_deadline()


async def current_time():
    return trio.current_time()


#
# Task groups
#

class ExceptionGroup(BaseExceptionGroup, trio.MultiError):
    pass


class TaskGroup:
    __slots__ = '_active', '_nursery_manager', '_nursery', 'cancel_scope'

    def __init__(self) -> None:
        self._active = False
        self._nursery_manager = trio.open_nursery()
        self.cancel_scope = None

    async def __aenter__(self):
        self._active = True
        self._nursery = await self._nursery_manager.__aenter__()
        self.cancel_scope = CancelScope(self._nursery.cancel_scope)
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        try:
            return await self._nursery_manager.__aexit__(exc_type, exc_val, exc_tb)
        except trio.MultiError as exc:
            raise ExceptionGroup(exc.exceptions) from None
        finally:
            self._active = False

    async def spawn(self, func: Callable, *args, name=None) -> None:
        if not self._active:
            raise RuntimeError('This task group is not active; no new tasks can be spawned.')

        self._nursery.start_soon(func, *args, name=name)


AbstractTaskGroup.register(TaskGroup)


#
# Threads
#

async def run_in_thread(func: Callable[..., T_Retval], *args, cancellable: bool = False,
                        limiter: Optional['CapacityLimiter'] = None) -> T_Retval:
    def wrapper():
        with claim_worker_thread('trio'):
            return func(*args)

    trio_limiter = getattr(limiter, '_limiter', None)
    return await run_sync(wrapper, cancellable=cancellable, limiter=trio_limiter)

run_async_from_thread = trio.from_thread.run


class BlockingPortal(AbstractBlockingPortal):
    __slots__ = '_token'

    def __init__(self):
        super().__init__()
        self._token = trio.lowlevel.current_trio_token()

    def _spawn_task_from_thread(self, func: Callable, args: tuple, future: Future) -> None:
        return trio.from_thread.run(self._task_group.spawn, self._call_func, func, args, future,
                                    trio_token=self._token)


#
# Stream wrappers
#

@attr.s(slots=True, frozen=True, auto_attribs=True)
class ReceiveStreamWrapper(ReceiveByteStream):
    _stream: trio.abc.ReceiveStream

    async def receive(self, max_bytes: Optional[int] = None) -> bytes:
        return await self._stream.receive_some(max_bytes)

    async def aclose(self) -> None:
        await self._stream.aclose()


@attr.s(slots=True, frozen=True, auto_attribs=True)
class SendStreamWrapper(SendByteStream):
    _stream: trio.abc.SendStream

    async def send(self, item: bytes) -> None:
        await self._stream.send_all(item)

    async def aclose(self) -> None:
        await self._stream.aclose()


#
# Subprocesses
#

@attr.s(slots=True, auto_attribs=True)
class Process(AbstractAsyncProcess):
    _process: trio.Process
    _stdin: Optional[SendByteStream]
    _stdout: Optional[ReceiveByteStream]
    _stderr: Optional[ReceiveByteStream]

    async def wait(self) -> int:
        return await self._process.wait()

    def terminate(self) -> None:
        self._process.terminate()

    def kill(self) -> None:
        self._process.kill()

    def send_signal(self, signal: int) -> None:
        self._process.send_signal(signal)

    @property
    def pid(self) -> int:
        return self._process.pid

    @property
    def returncode(self) -> Optional[int]:
        return self._process.returncode

    @property
    def stdin(self) -> Optional[SendByteStream]:
        return self._stdin

    @property
    def stdout(self) -> Optional[ReceiveByteStream]:
        return self._stdout

    @property
    def stderr(self) -> Optional[ReceiveByteStream]:
        return self._stderr


async def open_process(command, *, shell: bool, stdin: int, stdout: int, stderr: int):
    process = await trio.open_process(command, stdin=stdin, stdout=stdout, stderr=stderr,
                                      shell=shell)
    stdin_stream = SendStreamWrapper(process.stdin) if process.stdin else None
    stdout_stream = ReceiveStreamWrapper(process.stdout) if process.stdout else None
    stderr_stream = ReceiveStreamWrapper(process.stderr) if process.stderr else None
    return Process(process, stdin_stream, stdout_stream, stderr_stream)


#
# Sockets and networking
#

@attr.s(slots=True, kw_only=True, auto_attribs=True)
class StreamMixin:
    raw_socket: socket.SocketType
    _trio_socket: trio.socket.SocketType

    async def aclose(self) -> None:
        self._trio_socket.close()

    async def receive(self, max_bytes: Optional[int] = None) -> bytes:
        try:
            return await self._trio_socket.recv(max_bytes or 65536)
        except trio.ClosedResourceError:
            raise ClosedResourceError from None
        except trio.BusyResourceError:
            raise BusyResourceError('receiving from') from None
        except (ConnectionResetError, OSError) as exc:
            self.raw_socket.close()
            raise ClosedResourceError from exc

    async def send(self, item: bytes) -> None:
        view = memoryview(item)
        total = len(item)
        sent = 0
        try:
            while sent < total:
                sent += await self._trio_socket.send(view[sent:])
        except trio.ClosedResourceError:
            raise ClosedResourceError from None
        except trio.BusyResourceError:
            raise BusyResourceError('sending from') from None
        except (ConnectionResetError, OSError) as exc:
            self.raw_socket.close()
            raise ClosedResourceError from exc


@attr.s(slots=True, auto_attribs=True)
class ListenerMixin:
    stream_class: ClassVar[Type[ByteStream]]
    raw_socket: socket.SocketType
    _trio_socket: trio.socket.SocketType

    async def aclose(self):
        self._trio_socket.close()

    async def accept(self):
        try:
            trio_socket, address = await self._trio_socket.accept()
        except OSError as exc:
            if exc.errno in (9, 10038):
                raise ClosedResourceError from None
            else:
                raise
        except trio.ClosedResourceError:
            raise ClosedResourceError from None
        except trio.BusyResourceError:
            raise BusyResourceError('accepting connections from') from None

        return self.stream_class(raw_socket=trio_socket._sock, trio_socket=trio_socket)


@attr.s(slots=True, auto_attribs=True)
class TCPSocketStream(StreamMixin, AbstractTCPSocketStream):
    pass


@attr.s(slots=True, auto_attribs=True)
class TCPListener(ListenerMixin, AbstractTCPListener):
    stream_class = TCPSocketStream


@attr.s(slots=True, auto_attribs=True)
class UNIXSocketStream(StreamMixin, AbstractUNIXSocketStream):
    pass


@attr.s(slots=True, auto_attribs=True)
class UNIXListener(ListenerMixin, AbstractUNIXListener):
    stream_class = UNIXSocketStream


async def connect_tcp(raw_socket: socket.SocketType,
                      remote_address: tuple) -> AbstractTCPSocketStream:
    trio_socket = trio.socket.from_stdlib_socket(raw_socket)
    await trio_socket.connect(remote_address)
    return TCPSocketStream(raw_socket=raw_socket, trio_socket=trio_socket)


async def connect_unix(raw_socket: socket.SocketType,
                       remote_address: str) -> AbstractUNIXSocketStream:
    trio_socket = trio.socket.from_stdlib_socket(raw_socket)
    await trio_socket.connect(remote_address)
    return UNIXSocketStream(raw_socket=raw_socket, trio_socket=trio_socket)


async def create_tcp_listener(raw_socket: socket.SocketType) -> AbstractTCPListener:
    trio_socket = trio.socket.from_stdlib_socket(raw_socket)
    return TCPListener(raw_socket=raw_socket, trio_socket=trio_socket)


async def create_unix_listener(raw_socket: socket.SocketType) -> AbstractUNIXListener:
    trio_socket = trio.socket.from_stdlib_socket(raw_socket)
    return UNIXListener(raw_socket=raw_socket, trio_socket=trio_socket)


class UDPSocket(AbstractUDPSocket):
    def __init__(self, raw_socket: socket.SocketType):
        self.raw_socket = raw_socket
        self._trio_socket = trio.socket.from_stdlib_socket(raw_socket)

    async def aclose(self) -> None:
        self._trio_socket.close()

    async def receive(self) -> UDPPacket:
        try:
            return UDPPacket(*await self._trio_socket.recvfrom(65536))
        except trio.ClosedResourceError:
            raise ClosedResourceError from None
        except trio.BusyResourceError:
            raise BusyResourceError('receiving from') from None
        except OSError:
            if self.raw_socket.fileno() < 0:
                raise ClosedResourceError from None
            else:
                raise

    async def send(self, item: UDPPacket) -> None:
        try:
            await self._trio_socket.sendto(*item)
        except trio.ClosedResourceError:
            raise ClosedResourceError from None
        except trio.BusyResourceError:
            raise BusyResourceError('sending from') from None
        except OSError:
            if self.raw_socket.fileno() < 0:
                raise ClosedResourceError from None


class ConnectedUDPSocket(AbstractConnectedUDPSocket):
    def __init__(self, raw_socket: socket.SocketType):
        self.raw_socket = raw_socket
        self._trio_socket = trio.socket.from_stdlib_socket(raw_socket)

    async def aclose(self) -> None:
        self._trio_socket.close()

    async def receive(self) -> bytes:
        try:
            return await self._trio_socket.recv(65536)
        except trio.ClosedResourceError:
            raise ClosedResourceError from None
        except trio.BusyResourceError:
            raise BusyResourceError('receiving from') from None
        except OSError:
            if self.raw_socket.fileno() < 0:
                raise ClosedResourceError from None
            else:
                raise

    async def send(self, item: bytes) -> None:
        try:
            await self._trio_socket.send(item)
        except trio.ClosedResourceError:
            raise ClosedResourceError from None
        except trio.BusyResourceError:
            raise BusyResourceError('sending from') from None
        except OSError:
            if self.raw_socket.fileno() < 0:
                raise ClosedResourceError from None
            else:
                raise


async def create_udp_socket(raw_socket: socket.SocketType):
    try:
        raw_socket.getpeername()
    except OSError:
        return UDPSocket(raw_socket)
    else:
        return ConnectedUDPSocket(raw_socket)

#
# Synchronization
#

Lock = trio.Lock


class Event:
    def __init__(self):
        self._event = trio.Event()

    async def set(self) -> None:
        self._event.set()

    def clear(self):
        if self._event.is_set():
            self._event = trio.Event()

    def is_set(self) -> bool:
        return self._event.is_set()

    async def wait(self):
        await self._event.wait()


class Condition:
    def __init__(self, lock: Optional[trio.Lock] = None):
        self._cond = trio.Condition(lock=lock)

    async def __aenter__(self):
        await self._cond.__aenter__()

    async def __aexit__(self, *exc_info):
        return await self._cond.__aexit__(*exc_info)

    async def notify(self, n: int = 1) -> None:
        self._cond.notify(n)

    async def notify_all(self) -> None:
        self._cond.notify_all()

    def locked(self):
        return self._cond.locked()

    async def wait(self):
        return await self._cond.wait()


Semaphore = trio.Semaphore


class Queue:
    def __init__(self, max_items: int) -> None:
        max_buffer_size = max_items if max_items > 0 else math.inf
        self._send_channel, self._receive_channel = trio.open_memory_channel(max_buffer_size)

    def empty(self):
        return self._receive_channel.statistics().current_buffer_used == 0

    def full(self):
        statistics = self._receive_channel.statistics()
        return statistics.current_buffer_used >= statistics.max_buffer_size

    def qsize(self) -> int:
        return self._receive_channel.statistics().current_buffer_used

    async def put(self, item) -> None:
        await self._send_channel.send(item)

    async def get(self):
        return await self._receive_channel.receive()

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self._receive_channel.receive()


class CapacityLimiter(AbstractCapacityLimiter):
    def __init__(self, limiter_or_tokens: Union[float, trio.CapacityLimiter]):
        if isinstance(limiter_or_tokens, trio.CapacityLimiter):
            self._limiter = limiter_or_tokens
        else:
            self._limiter = trio.CapacityLimiter(limiter_or_tokens)

    async def __aenter__(self) -> 'CapacityLimiter':
        await self._limiter.__aenter__()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self._limiter.__aexit__(exc_type, exc_val, exc_tb)

    @property
    def total_tokens(self) -> float:
        return self._limiter.total_tokens

    async def set_total_tokens(self, value: float) -> None:
        self._limiter.total_tokens = value

    @property
    def borrowed_tokens(self) -> int:
        return self._limiter.borrowed_tokens

    @property
    def available_tokens(self) -> float:
        return self._limiter.available_tokens

    async def acquire_nowait(self):
        return self.acquire_nowait()

    async def acquire_on_behalf_of_nowait(self, borrower):
        try:
            return self._limiter.acquire_on_behalf_of_nowait(borrower)
        except trio.WouldBlock as exc:
            raise WouldBlock from exc

    def acquire(self):
        return self._limiter.acquire()

    def acquire_on_behalf_of(self, borrower):
        return self._limiter.acquire_on_behalf_of(borrower)

    async def release(self):
        self._limiter.release()

    async def release_on_behalf_of(self, borrower):
        self._limiter.release_on_behalf_of(borrower)


def current_default_thread_limiter():
    native_limiter = trio.to_thread.current_default_thread_limiter()
    return CapacityLimiter(native_limiter)


@attr.s(auto_attribs=True, init=False, slots=True)
class MemoryMessageStream(Generic[T_Item], MessageStream[T_Item]):
    _receive_channel: trio.abc.ReceiveChannel[T_Item]
    _send_channel: trio.abc.SendChannel[T_Item]

    def __init__(self):
        self._send_channel, self._receive_channel = trio.open_memory_channel(0)

    async def receive(self) -> T_Item:
        try:
            return await self._receive_channel.receive()
        except (trio.EndOfChannel, trio.ClosedResourceError, trio.BrokenResourceError):
            raise ClosedResourceError from None

    async def send(self, item: T_Item) -> None:
        try:
            await self._send_channel.send(item)
        except (trio.EndOfChannel, trio.ClosedResourceError, trio.BrokenResourceError):
            raise ClosedResourceError from None

    async def aclose(self) -> None:
        await self._send_channel.aclose()
        await self._receive_channel.aclose()


AbstractLock.register(Lock)
AbstractCondition.register(Condition)
AbstractEvent.register(Event)
AbstractSemaphore.register(Semaphore)
AbstractCapacityLimiter.register(CapacityLimiter)


#
# Signal handling
#

@asynccontextmanager
async def receive_signals(*signals: int):
    with trio.open_signal_receiver(*signals) as cm:
        yield cm


#
# Testing and debugging
#

async def get_current_task() -> TaskInfo:
    task = trio.lowlevel.current_task()

    parent_id = None
    if task.parent_nursery and task.parent_nursery.parent_task:
        parent_id = id(task.parent_nursery.parent_task)

    return TaskInfo(id(task), parent_id, task.name, task.coro)


async def get_running_tasks() -> List[TaskInfo]:
    root_task = trio.lowlevel.current_root_task()
    task_infos = [TaskInfo(id(root_task), None, root_task.name, root_task.coro)]
    nurseries = root_task.child_nurseries
    while nurseries:
        new_nurseries = []  # type: List[trio.Nursery]
        for nursery in nurseries:
            for task in nursery.child_tasks:
                task_infos.append(
                    TaskInfo(id(task), id(nursery.parent_task), task.name, task.coro))
                new_nurseries.extend(task.child_nurseries)

        nurseries = new_nurseries

    return task_infos


def wait_all_tasks_blocked():
    import trio.testing
    return trio.testing.wait_all_tasks_blocked()


class TestRunner(AbstractTestRunner):
    def __init__(self, **options):
        from collections import deque
        from queue import Queue

        self._call_queue = Queue()
        self._result_queue = deque()
        self._stop_event: Optional[trio.Event] = None
        self._nursery: Optional[trio.Nursery] = None
        self._options = options

    async def _trio_main(self) -> None:
        self._stop_event = trio.Event()
        async with trio.open_nursery() as self._nursery:
            await self._stop_event.wait()

    async def _call_func(self, func, args, kwargs):
        try:
            retval = await func(*args, **kwargs)
        except BaseException as exc:
            self._result_queue.append(Error(exc))
            raise
        else:
            self._result_queue.append(Value(retval))

    def _main_task_finished(self, outcome) -> None:
        self._nursery = None

    def close(self) -> None:
        if self._stop_event:
            self._stop_event.set()
            while self._nursery is not None:
                self._call_queue.get()()

    def call(self, func: Callable[..., Awaitable], *args, **kwargs):
        if self._nursery is None:
            trio.lowlevel.start_guest_run(
                self._trio_main, run_sync_soon_threadsafe=self._call_queue.put,
                done_callback=self._main_task_finished, **self._options)
            while self._nursery is None:
                self._call_queue.get()()

        self._nursery.start_soon(self._call_func, func, args, kwargs)
        while not self._result_queue:
            self._call_queue.get()()

        outcome = self._result_queue.pop()
        return outcome.unwrap()
