"""阶段副作用的租约保活；失败或取消必须回收并等待所有子协程。"""

import asyncio


async def execute_with_stage_lease(operation, *, renew, interval_seconds):
    async def heartbeat():
        while True:
            try:
                await asyncio.wait_for(asyncio.Event().wait(), timeout=interval_seconds)
            except TimeoutError:
                try:
                    await renew()
                except Exception as exc:
                    # 数据库超时也属于租约失效，不能被上层当作浏览器暂态错误重试。
                    raise RuntimeError("WEB_STAGE_LEASE_LOST") from exc

    effect = asyncio.create_task(operation, name="web-stage-effect")
    lease = asyncio.create_task(heartbeat(), name="web-stage-lease")
    try:
        done, _ = await asyncio.wait((effect, lease), return_when=asyncio.FIRST_COMPLETED)
        if lease in done:
            # 续租失败优先于业务完成；禁止把失效执行结果交给结算。
            await lease
        return await effect
    finally:
        for task in (lease, effect):
            if not task.done():
                task.cancel()
        await asyncio.gather(lease, effect, return_exceptions=True)
