import gevent.core
import gevent.event
import gevent.hub
import greenlet
import logging
import signal
import sys
# FIXME: gevent monkey patching
import socket
import threading

try:
    import asyncio

    if sys.platform == 'win32':
        from asyncio.windows_utils import socketpair
    else:
        socketpair = socket.socketpair
except ImportError:
    import trollius as asyncio

    if sys.platform == 'win32':
        from trollius.windows_utils import socketpair
    else:
        socketpair = socket.socketpair

logger = logging.getLogger('aiogreen')
_PY3 = sys.version_info >= (3,)

# FIXME: gevent monkey patching
if False:
    # trollius must use call original socket and threading functions.
    # Examples: socket.socket(), socket.socketpair(),
    # threading.current_thread().
    asyncio.base_events.socket = socket
    asyncio.events.threading = threading
    if sys.platform == 'win32':
        asyncio.windows_events.socket = socket
        asyncio.windows_utils.socket = socket
    else:
        asyncio.unix_events.socket = socket
        asyncio.unix_events.threading = threading
    # FIXME: patch also trollius.py3_ssl

    # BaseDefaultEventLoopPolicy._Local must inherit from threading.local
    # of the original threading module, not the patched threading module
    class _Local(threading.local):
        _loop = None
        _set_called = False

    asyncio.events.BaseDefaultEventLoopPolicy._Local = _Local

_EVENT_READ = asyncio.selectors.EVENT_READ
_EVENT_WRITE = asyncio.selectors.EVENT_WRITE

# gevent 1.0 or newer?
GEVENT10 = hasattr(gevent.hub.get_hub(), 'loop')


class _Selector(asyncio.selectors._BaseSelectorImpl):
    def __init__(self, loop):
        super(_Selector, self).__init__()
        # fd => events
        self._notified = {}
        self._loop = loop
        # gevent.event.Event() used by FD notifiers to wake up select()
        self._event = None
        self._gevent_events = {}
        if GEVENT10:
            self._gevent_loop = gevent.hub.get_hub().loop


    def close(self):
        keys = list(self.get_map().values())
        for key in keys:
            self.unregister(key.fd)
        super(_Selector, self).close()

    def _notify(self, fd, event):
        if fd in self._notified:
            self._notified[fd] |= event
        else:
            self._notified[fd] = event
        if self._event is not None:
            # wakeup the select() method
            self._event.set()

    # FIXME: what is x?
    def _notify_read(self, event, x):
        self._notify(event.fd, _EVENT_READ)

    def _notify_write(self, event, x):
        self._notify(event.fd, _EVENT_WRITE)

    def _throwback(self, fd):
        # FIXME: do something with the FD in this case?
        pass

    def _read_events(self):
        notified = self._notified
        self._notified = {}
        ready = []
        for fd, events in notified.items():
            key = self.get_key(fd)
            ready.append((key, events & key.events))
        return ready

    def register(self, fileobj, events, data=None):
        key = super(_Selector, self).register(fileobj, events, data)
        event_dict = {}
        for event in (_EVENT_READ, _EVENT_WRITE):
            if not(events & event):
                continue

            if GEVENT10:
                if event == _EVENT_READ:
                    def func():
                        self._notify(key.fd, _EVENT_READ)
                    watcher = self._gevent_loop.io(key.fd, 1)
                    watcher.start(func)
                else:
                    def func():
                        self._notify(key.fd, _EVENT_WRITE)
                    watcher = self._gevent_loop.io(key.fd, 2)
                    watcher.start(func)
                event_dict[event] = watcher
            else:
                if event == _EVENT_READ:
                    gevent_event = gevent.core.read_event(key.fd, self._notify_read)
                else:
                    gevent_event = gevent.core.write_event(key.fd, self._notify_write)
                event_dict[event] = gevent_event

        self._gevent_events[key.fd] = event_dict
        return key

    def unregister(self, fileobj):
        key = super(_Selector, self).unregister(fileobj)
        event_dict = self._gevent_events.pop(key.fd, {})
        for event in (_EVENT_READ, _EVENT_WRITE):
            try:
                gevent_event = event_dict[event]
            except KeyError:
                continue
            if GEVENT10:
                gevent_event.stop()
            else:
                gevent_event.cancel()
        return key

    def select(self, timeout):
        events = self._read_events()
        if events:
            return events

        self._event = gevent.event.Event()
        try:
            if timeout is not None:
                def timeout_cb(event):
                    if event.ready():
                        return
                    event.set()

                gevent.spawn_later(timeout, timeout_cb, self._event)

                self._event.wait()
                # FIXME: cancel the timeout_cb if wait() returns 'ready'?
            else:
                # blocking call
                self._event.wait()
            return self._read_events()
        finally:
            self._event = None


# FIXME: support gevent 1.0
class EventLoop(asyncio.SelectorEventLoop):
    def __init__(self):
        self._greenlet = None
        selector = _Selector(self)
        super(EventLoop, self).__init__(selector=selector)

    def call_soon(self, callback, *args):
        handle = super(EventLoop, self).call_soon(callback, *args)
        if self._selector is not None and self._selector._event:
            # selector.select() is running: write into the self-pipe to wake up
            # the selector
            self._write_to_self()
        return handle

    def call_at(self, when, callback, *args):
        handle = super(EventLoop, self).call_at(when, callback, *args)
        if self._selector is not None and self._selector._event:
            # selector.select() is running: write into the self-pipe to wake up
            # the selector
            self._write_to_self()
        return handle

    def link_future(self, future):
        """Wait for a future, a task, or a coroutine object from a greenthread.

        Return the result or raise the exception of the future.

        The function must not be called from the greenthread
        of the aiogreen event loop.
        """
        if self._greenlet == gevent.getcurrent():
            raise RuntimeError("link_future() must not be called from "
                               "the greenthread of the aiogreen event loop")

        future = asyncio.async(future, loop=self)
        event = gevent.event.Event()

        def done(fut):
            try:
                result = fut.result()
            except Exception as exc:
                event.send_exception(exc)
            else:
                event.send(result)

        future.add_done_callback(done)
        return event.wait()

    def wrap_greenthread(self, gt):
        """Wrap an a greenlet into a Future object.

        The Future object waits for the completion of a greenthread. The result
        or the exception of the greenthread will be stored in the Future object.

        The greenthread must be wrapped before its execution starts. If the
        greenthread is running or already finished, an exception is raised.

        For greenlets, the run attribute must be set.
        """
        fut = asyncio.Future(loop=self)

        if not isinstance(gt, greenlet.greenlet):
            raise TypeError("greenthread or greenlet request, not %s"
                            % type(gt))

        if gt.dead:
            raise RuntimeError("wrap_greenthread: the greenthread already finished")

        if isinstance(gt, gevent.Greenlet):
            # Don't use gevent.Greenlet.__bool__() because since gevent 1.0, a
            # greenlet is True if it already starts, and gevent.spawn() starts
            # the greenlet just after its creation.
            if _PY3:
                is_running = greenlet.greenlet.__bool__
            else:
                is_running = greenlet.greenlet.__nonzero__
            if is_running(gt):
                raise RuntimeError("wrap_greenthread: the greenthread is running")

            orig_func = gt._run
            def wrap_func(*args, **kw):
                try:
                    result = orig_func(*args, **kw)
                except Exception as exc:
                    fut.set_exception(exc)
                else:
                    fut.set_result(result)
            gt._run = wrap_func
        else:
            if gt:
                raise RuntimeError("wrap_greenthread: the greenthread is running")

            try:
                orig_func = gt.run
            except AttributeError:
                raise RuntimeError("wrap_greenthread: the run attribute "
                                   "of the greenlet is not set")
            def wrap_func(*args, **kw):
                try:
                    result = orig_func(*args, **kw)
                except Exception as exc:
                    fut.set_exception(exc)
                else:
                    fut.set_result(result)
            gt.run = wrap_func
        return fut

    def run_forever(self):
        self._greenlet = gevent.getcurrent()
        try:
            super(EventLoop, self).run_forever()
        finally:
            self._greenlet = None


class EventLoopPolicy(asyncio.DefaultEventLoopPolicy):
    _loop_factory = EventLoop