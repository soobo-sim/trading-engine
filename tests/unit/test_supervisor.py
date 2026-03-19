"""TaskSupervisor 유닛 테스트."""

import asyncio

import pytest
import pytest_asyncio

from core.task.supervisor import TaskSupervisor


@pytest_asyncio.fixture
async def supervisor():
    sup = TaskSupervisor()
    yield sup
    await sup.stop_all()


class TestTaskSupervisor:
    @pytest.mark.asyncio
    async def test_register_and_running(self, supervisor: TaskSupervisor):
        """태스크 등록 후 running 상태 확인."""
        ran = asyncio.Event()

        async def my_task():
            ran.set()
            await asyncio.sleep(10)  # 오래 실행

        await supervisor.register("test_task", my_task, auto_restart=False)
        await asyncio.sleep(0.05)

        assert supervisor.is_running("test_task")
        assert "test_task" in supervisor.running_names()
        assert ran.is_set()

    @pytest.mark.asyncio
    async def test_stop_single(self, supervisor: TaskSupervisor):
        """단일 태스크 중지."""
        async def my_task():
            await asyncio.sleep(10)

        await supervisor.register("to_stop", my_task, auto_restart=False)
        assert supervisor.is_running("to_stop")

        await supervisor.stop("to_stop")
        assert not supervisor.is_running("to_stop")

    @pytest.mark.asyncio
    async def test_stop_all(self, supervisor: TaskSupervisor):
        """전체 태스크 종료."""
        async def my_task():
            await asyncio.sleep(10)

        await supervisor.register("a", my_task, auto_restart=False)
        await supervisor.register("b", my_task, auto_restart=False)
        assert supervisor.task_count == 2

        await supervisor.stop_all()
        assert supervisor.alive_count == 0

    @pytest.mark.asyncio
    async def test_duplicate_name_replaces(self, supervisor: TaskSupervisor):
        """동일 이름 등록 시 기존 태스크 교체."""
        calls = []

        async def task_v1():
            calls.append("v1")
            await asyncio.sleep(10)

        async def task_v2():
            calls.append("v2")
            await asyncio.sleep(10)

        await supervisor.register("dup", task_v1, auto_restart=False)
        await asyncio.sleep(0.05)
        await supervisor.register("dup", task_v2, auto_restart=False)
        await asyncio.sleep(0.05)

        assert "v1" in calls
        assert "v2" in calls
        assert supervisor.task_count == 1

    @pytest.mark.asyncio
    async def test_auto_restart_on_exception(self, supervisor: TaskSupervisor):
        """예외 시 자동 재시작 + 재시작 횟수 추적."""
        attempts = []

        async def failing_task():
            attempts.append(1)
            raise RuntimeError("boom")

        await supervisor.register(
            "flaky", failing_task,
            max_restarts=3, auto_restart=True,
        )
        # backoff: 1초, 2초, 4초... 하지만 max_restarts=3이면 3번째에서 포기
        # 짧은 테스트를 위해 충분히 대기
        await asyncio.sleep(5.0)

        assert len(attempts) >= 3
        health = supervisor.get_health()
        assert "flaky" in health
        assert health["flaky"]["restarts"] >= 2
        assert "boom" in (health["flaky"].get("last_error") or "")

    @pytest.mark.asyncio
    async def test_normal_completion(self, supervisor: TaskSupervisor):
        """정상 종료 — 재시작 안 함."""
        async def quick_task():
            return  # 즉시 종료

        await supervisor.register("quick", quick_task, auto_restart=True)
        await asyncio.sleep(0.1)

        assert not supervisor.is_running("quick")
        health = supervisor.get_health()
        assert health["quick"]["restarts"] == 0

    @pytest.mark.asyncio
    async def test_health_report(self, supervisor: TaskSupervisor):
        async def alive_task():
            await asyncio.sleep(10)

        await supervisor.register("health_test", alive_task, auto_restart=False)
        await asyncio.sleep(0.05)

        health = supervisor.get_health()
        assert "health_test" in health
        assert health["health_test"]["alive"] is True
        assert "started_at" in health["health_test"]

    @pytest.mark.asyncio
    async def test_stop_group(self, supervisor: TaskSupervisor):
        """그룹(pair) 단위 중지."""
        async def task():
            await asyncio.sleep(10)

        await supervisor.register("trend_candle:xrp_jpy", task, auto_restart=False)
        await supervisor.register("trend_stoploss:xrp_jpy", task, auto_restart=False)
        await supervisor.register("trend_candle:btc_jpy", task, auto_restart=False)

        await supervisor.stop_group("xrp_jpy")

        assert not supervisor.is_running("trend_candle:xrp_jpy")
        assert not supervisor.is_running("trend_stoploss:xrp_jpy")
        assert supervisor.is_running("trend_candle:btc_jpy")
