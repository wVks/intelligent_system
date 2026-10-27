"""Request termination must finish its work and connection monitor before returning."""

import asyncio

import pytest

from studynavigator.agent import AgentError
from studynavigator.main import connected_request


class Connection:
    def __init__(self):
        self.disconnected = False
        self.checked = asyncio.Event()
        self.monitor = None

    async def is_disconnected(self):
        self.monitor = asyncio.current_task()
        self.checked.set()
        return self.disconnected


class PendingWork:
    """A write that can still be stopped, with asynchronous resource cleanup."""

    def __init__(self):
        self.started = asyncio.Event()
        self.finish = asyncio.Event()
        self.closed = asyncio.Event()
        self.task = None
        self.committed = False
        self.result = {"saved": True}

    async def run(self):
        self.task = asyncio.current_task()
        self.started.set()
        try:
            await self.finish.wait()
            self.committed = True
            return self.result
        finally:
            await asyncio.sleep(0)
            self.closed.set()

    def assert_stopped(self, connection):
        assert self.closed.is_set(), "Request returned before work released its resources"
        assert self.task.done()
        assert connection.monitor.done(), "Connection monitor outlived the request"


async def wait_until_running(work, connection):
    await asyncio.wait_for(asyncio.gather(work.started.wait(), connection.checked.wait()), 1)


@pytest.mark.asyncio
async def test_request_timeout_stops_work_before_it_can_commit():
    work, connection = PendingWork(), Connection()
    with pytest.raises(AgentError) as error:
        await connected_request(connection, work.run(), timeout=0.02)
    assert error.value.code == "request_timeout"
    work.assert_stopped(connection)
    assert not work.committed


@pytest.mark.asyncio
async def test_disconnect_stops_work_and_reaps_connection_monitor():
    work, connection = PendingWork(), Connection()
    request = asyncio.create_task(connected_request(connection, work.run(), timeout=2))
    try:
        await wait_until_running(work, connection)
        connection.disconnected = True
        with pytest.raises(AgentError) as error:
            await asyncio.wait_for(request, 1)
        assert error.value.code == "client_disconnected"
        work.assert_stopped(connection)
        assert not work.committed
    finally:
        request.cancel()
        await asyncio.gather(request, return_exceptions=True)


@pytest.mark.asyncio
async def test_outer_cancellation_is_preserved_after_async_work_cleanup():
    work, connection = PendingWork(), Connection()
    request = asyncio.create_task(connected_request(connection, work.run(), timeout=2))
    try:
        await wait_until_running(work, connection)
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
        work.assert_stopped(connection)
        assert not work.committed
    finally:
        request.cancel()
        await asyncio.gather(request, return_exceptions=True)


@pytest.mark.asyncio
async def test_completed_work_returns_its_result_without_leaving_a_monitor():
    work, connection = PendingWork(), Connection()
    request = asyncio.create_task(connected_request(connection, work.run(), timeout=2))
    try:
        await wait_until_running(work, connection)
        work.finish.set()
        assert await asyncio.wait_for(request, 1) is work.result
        work.assert_stopped(connection)
        assert work.committed
    finally:
        request.cancel()
        await asyncio.gather(request, return_exceptions=True)
